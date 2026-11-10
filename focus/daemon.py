"""focusd: the root daemon (contract §4, §6, §6b, §7, §8).

Threads: the main thread runs the reconcile loop and handles signals; the
socket server runs one thread per connection; list downloads run in
background threads. All state lives behind `Daemon.lock`. The judge runs
outside the lock so `status` stays responsive during a judge call.
"""

import collections
import json
import os
import pwd
import secrets
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback

from . import config, judge, paths, render, version
from .config import penalty_domain, under_any, url_host
from .lists import Lists, dnslog_check
from .state import EventLog, State, day_start, hhmm, next_day_start

SESSION_TTL = 600
MAX_REQUEST = 64 * 1024
IO_TIMEOUT = 10
VERDICTS = {"working", "procrastinating", "unclear", "allowed", "idle", "undecided", "error"}
OBSERVE_SOURCES = {"title", "vision", "unlock", "idle"}
PENALTY_SOURCES = {"title", "vision"}
JUDGE_CALL_EVENTS = {"request", "followup", "appeal"}


class Fail(Exception):
    """An error response: {"ok": false, "error": code, "message": message}."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _err(code, message):
    return {"ok": False, "error": code, "message": message}


def _str_field(req, key, maxlen, required=False):
    v = req.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str):
        raise Fail("bad_request", f"'{key}' must be a string")
    return v[:maxlen]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _stat(path):
    """Change marker for a file; lstat too, so swapping in a symlink counts."""
    out = []
    for fn in (os.lstat, os.stat):
        try:
            st = fn(path)
            out.append((st.st_mode, st.st_uid, st.st_mtime_ns, st.st_size, st.st_ino))
        except OSError:
            out.append(None)
    return tuple(out) if any(out) else None


class Session:
    """An in-memory judge conversation, bound to one group or penalty."""

    def __init__(self, kind, target, requested=None):
        self.id = secrets.token_hex(8)
        self.kind = kind              # "unlock" | "appeal"
        self.target = target          # group id | penalty id
        self.requested = requested    # requested minutes (unlock only)
        self.turns = []               # [theo_text, judge_question | None]
        self.touched = time.time()
        self.busy = False


class Daemon:
    def __init__(self):
        self.lock = threading.RLock()
        self.events = EventLog(paths.events_file())
        self.cfg = None
        self.state = State()
        self.sessions = {}
        self.lists = Lists(self.log, self.reconcile)
        self.writer = render.Writer(self.log)
        self.started = time.time()
        self.judge_calls = collections.deque()
        self.judge_procs = set()
        self.procs_lock = threading.Lock()
        self.watch_last = None
        self.watch_last_seen = None
        self.prev_observe = None
        self.stopping = False
        self.closed = False
        self.next_reconcile = 0.0
        self.wake = threading.Event()
        self.stop_requested = False
        self.hup_requested = False
        self.server = None
        self.serving = False
        self._counts_key = None
        self._counts = {}
        self._cfg_stat = self._policy_stat = None
        self.config_problem = None   # health line while the config is unusable
        self.judge_problem = None    # health line while claude is not logged in
        self.policy_problem = None   # health line while judge.md is missing/untrusted

    # ------------------------------------------------------------ helpers

    def log(self, type_, **fields):
        if self.closed:
            return None
        if type_.endswith("_error"):
            print(f"focusd: {type_}: {json.dumps(fields, ensure_ascii=False)}",
                  file=sys.stderr, flush=True)
        return self.events.append(type_, **fields)

    def health(self):
        return self.writer.health() + [h for h in (self.config_problem, self.policy_problem,
                                                   self.judge_problem) if h]

    def _set_policy_problem(self, reason):
        self.policy_problem = f"judge: policy file rejected: {reason}" if reason else None

    def _check_policy(self):
        """Validate judge.md now (start, reload, mtime change), not only at judge time."""
        self._set_policy_problem(judge.load_policy(paths.p(self.cfg.judge.policy_file))[1])

    def _events_group(self):
        try:
            self.events.gid = pwd.getpwnam(self.cfg.user).pw_gid
        except KeyError:
            self.events.gid = None

    def _save(self):
        try:
            self.state.save(paths.state_file())
        except OSError as e:
            print(f"focusd: cannot save state: {e}", file=sys.stderr)

    def _day_start(self, now):
        return day_start(now, self.cfg.day_starts_at)

    def locked_ids(self):
        return {gid for gid, g in self.cfg.groups.items()
                if g.enabled and gid not in self.state.unlocks}

    def used_map(self, now):
        used = collections.Counter()
        for ev in self.events.since(self._day_start(now), {"unlock"}):
            if _num(ev.get("minutes")):
                used[ev.get("group")] += int(ev["minutes"])
        return used

    def calls_last_hour(self, now):
        while self.judge_calls and self.judge_calls[0] <= now - 3600:
            self.judge_calls.popleft()
        return self.judge_calls

    def flags_today(self, now):
        return sum(1 for ev in self.events.since(self._day_start(now), {"observe"})
                   if ev.get("verdict") == "procrastinating")

    def penalty(self, pid):
        return next((p for p in self.state.penalties if p.get("id") == pid), None)

    def unlocked_url_hosts(self):
        return {url_host(u) for gid in self.state.unlocks if gid in self.cfg.groups
                for u in self.cfg.groups[gid].url_block}

    def penalty_domains(self):
        """Active penalty domains to render, re-validated against the current
        config, never overriding a judge's grant."""
        unlocked, never, out = self.unlocked_url_hosts(), self.cfg.never_block, []
        for p in sorted(self.state.penalties, key=lambda p: p["since"]):
            if p.get("domain") is None:
                continue
            d, _why = penalty_domain(p["domain"], never)
            if d and d not in out and not under_any(d, unlocked):
                out.append(d)
        return out

    def _sanitize_penalties(self):
        """Normalise penalty domains loaded from state.json; drop invalid ones."""
        changed = False
        for p in self.state.penalties:
            raw = p.get("domain")
            if raw is None:
                continue
            d, why = penalty_domain(raw, self.cfg.never_block)
            if d != raw:
                print(f"focusd: penalty {p.get('id')}: domain {raw!r} -> {d!r} ({why or 'normalised'})",
                      file=sys.stderr)
                p["domain"] = d
                changed = True
        if changed:
            self._save()

    def allowed_uids(self):
        uids = {0}
        try:
            uids.add(pwd.getpwnam(self.cfg.user).pw_uid)
        except KeyError:
            pass
        return uids

    def _track(self, proc, started):
        with self.procs_lock:
            (self.judge_procs.add if started else self.judge_procs.discard)(proc)

    # ------------------------------------------------------------ reconcile

    def reconcile(self):
        """Expire, render, write what differs (contract §4)."""
        with self.lock:
            if self.closed:
                return
            now = time.time()
            changed = False
            for gid, u in list(self.state.unlocks.items()):
                g = self.cfg.groups.get(gid)
                if u["until"] <= now or g is None or not g.enabled:
                    del self.state.unlocks[gid]
                    self.log("expire", group=gid)
                    changed = True
            for p in list(self.state.penalties):
                if p["until"] <= now:
                    self.state.penalties.remove(p)
                    self.log("penalty_end", id=p["id"], by="expire")
                    changed = True
            active = {p["id"] for p in self.state.penalties}
            for pid in [k for k in self.state.appeal_cooldowns if k not in active]:
                del self.state.appeal_cooldowns[pid]
                changed = True
            for gid in [k for k, t in self.state.cooldowns.items() if t < now - 86400]:
                del self.state.cooldowns[gid]
                changed = True
            for sid in [k for k, s in self.sessions.items()
                        if not s.busy and s.touched + SESSION_TTL < now]:
                del self.sessions[sid]
            if changed:
                self._save()
            locked, pens = self.locked_ids(), self.penalty_domains()
            block = self.writer.block(self.cfg, locked, self.lists, pens)
            self.writer.apply(self.cfg, block, render.policy_json(self.cfg, locked, pens))
            self.events.prune(now)
            self.next_reconcile = now + self.cfg.reconcile_seconds

    def _deadline_passed(self, now):
        return (any(u["until"] <= now for u in self.state.unlocks.values())
                or any(p["until"] <= now for p in self.state.penalties))

    def reload(self):
        self._cfg_stat = _stat(paths.config_file())
        cfg, errors, fatal = config.load(paths.config_file())
        with self.lock:
            for e in errors:
                self.log("config_error", error=e)
            if cfg is not None and not fatal:
                self.cfg = cfg
                self.config_problem = None
                self._events_group()
                self.log("config_reload")
                self.lists.load_cache(cfg)
            else:
                self.config_problem = "config: last reload failed, using previous config"
            self._policy_stat = _stat(paths.p(self.cfg.judge.policy_file))
            self._check_policy()
            self.reconcile()

    # ------------------------------------------------------------ lifecycle

    def run(self) -> int:
        for d in (paths.state_dir(), paths.lists_dir(), paths.judge_dir(), paths.run_dir()):
            paths.ensure_dir(d, 0o755)
        cfg, errors, fatal = config.load(paths.config_file())
        if cfg is None:
            print(f"focusd: no usable config: {'; '.join(errors)}", file=sys.stderr)
            return 1
        self.cfg = cfg
        try:
            self.server = self._bind()
        except (OSError, RuntimeError) as e:
            print(f"focusd: cannot open socket: {e}", file=sys.stderr)
            return 1
        now = time.time()
        if fatal:
            self.config_problem = "config: invalid config at start, enforcing what parsed"
        self._events_group()
        self.events.secure()
        last_type = self.events.open(now)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_stop)
        signal.signal(signal.SIGHUP, self._on_hup)
        self.log("start", version=version)
        if last_type is not None and last_type != "stop":
            self.log("unclean_start")
        for e in errors:
            self.log("config_error", error=e)
        self.state, err = State.load(paths.state_file())
        if err:
            print(f"focusd: {err}", file=sys.stderr)
        self._sanitize_penalties()
        self._save()          # rewrites state.json as 0600 (it holds reasons and titles)
        self.judge_calls.extend(sorted(e["t"] for e in self.events.since(now - 3600, JUDGE_CALL_EVENTS)))
        self.lists.load_cache(cfg)
        self._cfg_stat = _stat(paths.config_file())
        self._policy_stat = _stat(paths.p(cfg.judge.policy_file))
        self._check_policy()
        self.reconcile()
        self.lists.refresh(cfg, now)
        threading.Thread(target=self.server.serve_forever, name="socket", daemon=True).start()
        self.serving = True
        self.loop()
        self.shutdown()
        return 0

    def _on_stop(self, signum, frame):
        self.stop_requested = True
        self.wake.set()

    def _on_hup(self, signum, frame):
        self.hup_requested = True
        self.wake.set()

    def loop(self):
        while not self.stop_requested:
            self.wake.wait(1.0)
            self.wake.clear()
            if self.stop_requested:
                break
            try:
                if self.hup_requested or _stat(paths.config_file()) != self._cfg_stat:
                    self.hup_requested = False
                    self.reload()
                now = time.time()
                policy_stat = _stat(paths.p(self.cfg.judge.policy_file))
                with self.lock:
                    due = now >= self.next_reconcile or self._deadline_passed(now)
                if policy_stat != self._policy_stat:
                    with self.lock:
                        self._check_policy()
                if due or policy_stat != self._policy_stat:
                    self._policy_stat = policy_stat
                    self.reconcile()
                self.lists.refresh(self.cfg, now)
            except Exception:
                traceback.print_exc()

    def shutdown(self):
        """Fail closed: drop every unlock, write the fully locked files, stop."""
        if self.serving:
            self.server.shutdown()
        with self.lock:
            self.stopping = True
            for gid in list(self.state.unlocks):
                self.log("lock", group=gid, by="shutdown")
            self.state.unlocks.clear()
            self.sessions.clear()
            self._save()
            try:
                self.reconcile()
            except Exception:
                traceback.print_exc()
            with self.procs_lock:
                for proc in list(self.judge_procs):
                    judge.signal_group(proc)
            self.log("stop")
            self.closed = True
        if self.server:
            self.server.server_close()
            try:
                os.unlink(paths.socket_file())
            except OSError:
                pass

    def _bind(self):
        path = paths.socket_file()
        if os.path.lexists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1)
            try:
                probe.connect(path)
                in_use = True
            except OSError:
                in_use = False
            finally:
                probe.close()
            if in_use:
                raise RuntimeError(f"another focusd is listening on {path}")
            os.unlink(path)
        old = os.umask(0o117)
        try:
            server = _Server(path, _Handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o660)
        if paths.privileged():
            try:
                os.chown(path, 0, pwd.getpwnam(self.cfg.user).pw_gid)
            except KeyError:
                pass
        server.focusd = self
        return server

    # ------------------------------------------------------------ requests

    def dispatch(self, raw: bytes, uid: int) -> dict:
        try:
            if uid not in self.allowed_uids():
                return _err("forbidden", "not allowed to talk to focusd")
            try:
                req = json.loads(raw)
            except ValueError:
                return _err("bad_request", "request is not valid JSON")
            if not isinstance(req, dict):
                return _err("bad_request", "request must be a JSON object")
            op = req.get("op")
            fn = getattr(self, f"op_{op}") if isinstance(op, str) and op in OPS else None
            if fn is None:
                return _err("unknown_op", f"unknown op {op!r}")
            return fn(req)
        except Fail as f:
            return _err(f.code, f.message)
        except Exception as e:
            traceback.print_exc()
            return _err("internal", f"internal error: {type(e).__name__}")

    def op_status(self, req):
        with self.lock:
            now, cfg, st = time.time(), self.cfg, self.state
            used = self.used_map(now)
            counts_key = (cfg, self.lists.version)
            if self._counts_key is None or self._counts_key[0] is not cfg \
                    or self._counts_key[1] != self.lists.version:
                self._counts = {gid: render.group_host_count(cfg, g, self.lists.entries)
                                for gid, g in cfg.groups.items()}
                self._counts_key = counts_key
            live = {gid: u for gid, u in st.unlocks.items() if u["until"] > now}
            groups = []
            for gid, g in cfg.groups.items():
                u = live.get(gid)
                cd = st.cooldowns.get(gid)
                cd_until = cd + cfg.limits.deny_cooldown_minutes * 60 if cd is not None else None
                groups.append({
                    "id": gid, "label": g.label, "enabled": g.enabled,
                    "unlockable": g.unlockable, "locked": g.enabled and u is None,
                    "unlocked_until": u["until"] if u else None,
                    "used_today_min": used.get(gid, 0), "daily_minutes": cfg.daily(g),
                    "max_minutes": cfg.cap(g),
                    "cooldown_until": cd_until if cd_until and cd_until > now else None,
                    "webapp_classes": list(g.webapp_classes),
                    "hosts_count": self._counts.get(gid, 0),
                    "url_block": list(g.url_block), "url_allow": list(g.url_allow),
                    "title_patterns": list(g.title_patterns),
                })
            unlocks = [{"group": gid, "label": cfg.groups[gid].label, **u}
                       for gid, u in sorted(live.items(), key=lambda kv: kv[1]["until"])
                       if gid in cfg.groups]
            return {
                "ok": True, "version": version, "mode": cfg.mode, "now": now,
                "started": self.started, "day_start": self._day_start(now),
                "groups": groups, "unlocks": unlocks,
                "lists": self.lists.status(cfg),
                "judge_calls_last_hour": len(self.calls_last_hour(now)),
                "penalties": [dict(p) for p in sorted(st.penalties, key=lambda p: p["since"])
                              if p["until"] > now],
                "health": self.health(),
                "watch": {"mode": cfg.watch.mode,
                          "title_interval_seconds": cfg.watch.title_interval_seconds,
                          "last_seen": self.watch_last_seen,
                          "last": dict(self.watch_last) if self.watch_last else None,
                          "flags_today": self.flags_today(now)},
            }

    # ---- unlock / reply / appeal

    def _refused(self, sid, rule, message, retry_after=None, **target):
        self.log("refused", **target, rule=rule)
        self.sessions.pop(sid, None)
        resp = {"ok": True, "session": sid, "decision": "refused", "message": message,
                "rule": rule}
        if retry_after is not None:
            resp["retry_after"] = retry_after
        return resp

    def _group(self, gid):
        g = self.cfg.groups.get(gid)
        if g is None:
            self.log("refused", group=str(gid)[:64], rule="unknown_group")
            raise Fail("unknown_group", f"unknown group {gid!r}")
        if not g.enabled:
            self.log("refused", group=gid, rule="not_enabled")
            raise Fail("not_enabled", f"{g.label} is not enabled")
        return g

    def _rate_limit(self, now):
        calls = self.calls_last_hour(now)
        if len(calls) >= self.cfg.limits.judge_calls_per_hour:
            retry = calls[0] + 3600 if calls else None
            when = f" Try again after {hhmm(retry)}." if retry else ""
            return "rate_limit", f"Too many judge calls in the last hour.{when}", retry
        return None

    def _unlock_rules(self, g, now):
        """Hard rules 2–5 → (rule, message, retry_after) or None."""
        lim = self.cfg.limits
        if not g.unlockable:
            return "not_unlockable", f"{g.label} cannot be unlocked.", None
        cd = self.state.cooldowns.get(g.id)
        if cd is not None and now < cd + lim.deny_cooldown_minutes * 60:
            until = cd + lim.deny_cooldown_minutes * 60
            return "cooldown", f"{g.label} was denied recently. Try again after {hhmm(until)}.", until
        used, daily = self.used_map(now).get(g.id, 0), self.cfg.daily(g)
        if daily - used < 1:
            return ("budget", f"No {g.label} time left today ({used} of {daily} min used).",
                    next_day_start(now, self.cfg.day_starts_at))
        return self._rate_limit(now)

    @staticmethod
    def _text(req, key, lo):
        v = req.get(key)
        if not isinstance(v, str):
            raise Fail("bad_request", f"'{key}' must be a string")
        if not lo <= len(v.strip()) <= 2000:
            raise Fail("bad_request", f"'{key}' must be {lo}–2000 characters")
        return v.strip()

    def op_unlock(self, req):
        gid, reason, minutes = req.get("group"), req.get("reason"), req.get("minutes")
        if not isinstance(gid, str) or not isinstance(reason, str):
            raise Fail("bad_request", "'group' and 'reason' must be strings")
        if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool)
                                    or not 1 <= minutes <= 1440):
            raise Fail("bad_request", "'minutes' must be an integer from 1 to 1440")
        with self.lock:
            now = time.time()
            g = self._group(gid)
            sess = Session("unlock", gid, minutes)
            rule = self._unlock_rules(g, now)
            if rule:
                return self._refused(sess.id, *rule, group=gid)
            reason = self._text(req, "reason", 3)
            sess.turns.append([reason, None])
            self.sessions[sess.id] = sess
            self.log("request", session=sess.id, group=gid, reason=reason, requested=minutes)
            job = self._prepare_round(sess, now)
        return self._judge_round(sess, job)

    def op_appeal(self, req):
        pid, reason = req.get("penalty"), req.get("reason")
        if not isinstance(pid, str) or not isinstance(reason, str):
            raise Fail("bad_request", "'penalty' and 'reason' must be strings")
        with self.lock:
            now = time.time()
            p = self.penalty(pid)
            if p is None or p["until"] <= now:
                raise Fail("unknown_penalty", f"no active penalty {pid!r}")
            sess = Session("appeal", pid)
            rule = self._appeal_rules(pid, now)
            if rule:
                return self._refused(sess.id, *rule, penalty=pid)
            reason = self._text(req, "reason", 3)
            sess.turns.append([reason, None])
            self.sessions[sess.id] = sess
            self.log("appeal", session=sess.id, penalty=pid, reason=reason)
            job = self._prepare_round(sess, now)
        return self._judge_round(sess, job)

    def _appeal_rules(self, pid, now):
        cd = self.state.appeal_cooldowns.get(pid)
        until = cd + self.cfg.limits.deny_cooldown_minutes * 60 if cd is not None else 0
        if cd is not None and now < until:
            return "cooldown", f"This appeal was denied recently. Try again after {hhmm(until)}.", until
        return self._rate_limit(now)

    def op_reply(self, req):
        sid = req.get("session")
        if not isinstance(sid, str):
            raise Fail("bad_request", "'session' must be a string")
        with self.lock:
            now = time.time()
            sess = self.sessions.get(sid)
            if sess is None or (not sess.busy and sess.touched + SESSION_TTL < now):
                self.sessions.pop(sid, None)
                raise Fail("unknown_session", "unknown or expired session")
            if sess.busy:
                raise Fail("bad_request", "the judge is still deciding this session")
            answer = self._text(req, "answer", 1)
            if sess.kind == "unlock":
                try:
                    g = self._group(sess.target)
                except Fail:
                    self.sessions.pop(sid, None)
                    raise
                rule = self._unlock_rules(g, now)
                target = {"group": sess.target}
            else:
                p = self.penalty(sess.target)
                if p is None or p["until"] <= now:
                    self.sessions.pop(sid, None)
                    raise Fail("unknown_penalty", "the penalty is no longer active")
                rule = self._appeal_rules(sess.target, now)
                target = {"penalty": sess.target}
            if rule:
                return self._refused(sid, *rule, **target)
            sess.turns.append([answer, None])
            self.log("followup", session=sid, answer=answer)
            job = self._prepare_round(sess, now)
        return self._judge_round(sess, job)

    def _prepare_round(self, sess, now):
        """Under the lock: take a rate-limit slot and snapshot the prompt inputs.

        Returns (cfg, final, build) where build(context_text) -> prompt.
        """
        cfg = self.cfg
        final = len(sess.turns) >= cfg.limits.max_followups + 1
        self.judge_calls.append(now)
        sess.busy = True
        today = self.events.since(self._day_start(now))
        pens = [dict(p) for p in self.state.penalties]
        if sess.kind == "unlock":
            group = cfg.groups[sess.target]
            used = self.used_map(now).get(sess.target, 0)

            def build(ctx):
                return judge.unlock_prompt(
                    now=now, cfg=cfg, group=group, requested=sess.requested, used=used,
                    today_events=today, active_penalties=pens, turns=sess.turns,
                    final=final, context=ctx)
        else:
            penalty = dict(self.penalty(sess.target))

            def build(ctx):
                return judge.appeal_prompt(
                    now=now, cfg=cfg, penalty=penalty, today_events=today,
                    active_penalties=pens, turns=sess.turns, final=final, context=ctx)
        return cfg, final, build

    def _judge_round(self, sess, job):
        """Outside the lock: run the judge, then apply the verdict."""
        cfg, final, build = job
        verdict = error = kind = policy_reason = None
        policy_checked = False
        problems = []
        try:
            context, problems = judge.context_text([paths.p(f) for f in cfg.judge.context_files])
            sys_prompt, policy_reason = judge.system_prompt(paths.p(cfg.judge.policy_file))
            policy_checked = True
            if policy_reason:
                problems.append(f"judge policy file rejected: {policy_reason}")
            verdict = judge.run(cfg, build(context), sys_prompt, self._track)
        except judge.JudgeError as e:
            error, kind = str(e), e.kind
        except Exception as e:  # the judge must never take the daemon down
            traceback.print_exc()
            error = f"internal: {type(e).__name__}: {e}"
        with self.lock:
            for problem in problems:
                self.log("config_error", error=problem)
            if policy_checked:
                self._set_policy_problem(policy_reason)
            sess.busy = False
            sess.touched = time.time()
            if self.stopping:
                return {"ok": True, "session": sess.id, "decision": "error",
                        "message": "focusd is stopping."}
            if error is not None:
                self.sessions.pop(sess.id, None)
                self.log("judge_error", session=sess.id, error=error[:300])
                if kind == "not_logged_in":
                    self.judge_problem = "judge: not logged in"
                    message = error
                else:
                    message = f"The judge failed ({error[:120]}). Try again later."
                return {"ok": True, "session": sess.id, "decision": "error", "message": message}
            self.judge_problem = None
            if sess.kind == "unlock":
                return self._apply_unlock(sess, verdict, final)
            return self._apply_appeal(sess, verdict, final)

    def _apply_unlock(self, sess, v, final):
        now, cfg, gid = time.time(), self.cfg, sess.target
        decision, forced = v["decision"], v["decision"] == "ask" and final
        if forced:
            decision = "deny"
        extra = {"final_round_ask": True} if forced else {}
        self.log("verdict", session=sess.id, group=gid, decision=decision,
                 minutes=v["minutes"], message=v["message"], **extra)
        resp = {"ok": True, "session": sess.id, "decision": decision, "message": v["message"]}
        if decision == "ask":
            sess.turns[-1][1] = v["message"]
            return resp
        self.sessions.pop(sess.id, None)
        g = cfg.groups.get(gid)
        if g is None or not g.enabled:
            raise Fail("not_enabled", f"group {gid!r} is no longer enabled")
        if decision == "deny":
            self.state.cooldowns[gid] = now
            self._save()
            return resp
        used = self.used_map(now).get(gid, 0)
        remaining = cfg.daily(g) - used
        limits = [v["minutes"], cfg.cap(g), remaining]
        if sess.requested is not None:
            limits.append(sess.requested)
        granted = min(limits)
        if granted < 1:
            if remaining < 1:
                return self._refused(sess.id, "budget", f"No {g.label} time left today.",
                                     next_day_start(now, cfg.day_starts_at), group=gid)
            return self._refused(sess.id, "budget",
                                 f"The judge granted 0 minutes: {v['message']}", group=gid)
        u = self.state.unlocks.get(gid)
        until = max(u["until"], now) + granted * 60 if u else now + granted * 60
        reason = sess.turns[0][0]
        self.state.unlocks[gid] = {"since": u["since"] if u else now, "until": until,
                                   "minutes": (u["minutes"] if u else 0) + granted,
                                   "reason": reason}
        self.log("unlock", group=gid, minutes=granted, until=until, reason=reason,
                 session=sess.id)
        self._save()
        self.reconcile()
        return {**resp, "granted_minutes": granted, "until": until}

    def _apply_appeal(self, sess, v, final):
        now, pid = time.time(), sess.target
        decision = v["decision"]
        if decision == "ask" and final:
            decision = "deny"
        self.log("appeal_verdict", session=sess.id, penalty=pid, decision=decision,
                 message=v["message"])
        resp = {"ok": True, "session": sess.id, "decision": decision, "message": v["message"]}
        if decision == "ask":
            sess.turns[-1][1] = v["message"]
            return resp
        self.sessions.pop(sess.id, None)
        if decision == "deny":
            self.state.appeal_cooldowns[pid] = now
            self._save()
            return resp
        p = self.penalty(pid)
        if p is not None:
            self.state.penalties.remove(p)
            self.state.appeal_cooldowns.pop(pid, None)
            self.log("penalty_end", id=pid, by="appeal")
            self._save()
            self.reconcile()
        return resp

    # ---- lock / log / observe / penalize

    def op_lock(self, req):
        gid = req.get("group")
        if not isinstance(gid, str):
            raise Fail("bad_request", "'group' must be a group id or \"all\"")
        with self.lock:
            if gid != "all" and gid not in self.cfg.groups:
                raise Fail("unknown_group", f"unknown group {gid!r}")
            targets = [g for g in self.state.unlocks if gid in ("all", g)]
            for g in targets:
                del self.state.unlocks[g]
                self.log("lock", group=g, by="user")
            if targets:
                self._save()
                self.reconcile()
            return {"ok": True, "locked": targets}

    def op_log(self, req):
        limit, since, types = req.get("limit", 50), req.get("since"), req.get("types")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise Fail("bad_request", "'limit' must be an integer")
        if since is not None and not _num(since):
            raise Fail("bad_request", "'since' must be an epoch number")
        if types is not None and not (isinstance(types, list)
                                      and all(isinstance(t, str) for t in types)):
            raise Fail("bad_request", "'types' must be a list of strings")
        events = self.events.tail(max(1, min(limit, 500)), since, set(types) if types else None)
        return {"ok": True, "events": events}

    def op_observe(self, req):
        verdict, source = req.get("verdict"), req.get("source")
        if verdict not in VERDICTS or source not in OBSERVE_SOURCES:
            raise Fail("bad_request", "bad 'verdict' or 'source'")
        f = {k: _str_field(req, k, 300) for k in ("app", "class", "title", "description", "domain")}
        conf, streak = req.get("confidence"), req.get("streak")
        if conf is not None and not _num(conf):
            raise Fail("bad_request", "'confidence' must be a number")
        if streak is not None and (not isinstance(streak, int) or isinstance(streak, bool)):
            raise Fail("bad_request", "'streak' must be an integer")
        with self.lock:
            now = time.time()
            self.watch_last_seen = now
            self.watch_last = {"t": now, "verdict": verdict, "app": f["app"],
                               "title": f["title"], "source": source}
            key = (verdict, f["app"], f["title"])
            if verdict == "procrastinating" or key != self.prev_observe:
                self.log("observe", verdict=verdict, source=source, app=f["app"],
                         **{"class": f["class"]}, title=f["title"],
                         description=f["description"], domain=f["domain"],
                         confidence=conf, streak=streak)
            self.prev_observe = key
        return {"ok": True}

    def _validate_domain(self, raw, never, unlocked, now):
        """(domain, None) if the penalty domain is kept, else (None, reason)."""
        d, why = penalty_domain(raw, never)
        if d is None:
            return None, why
        if under_any(d, unlocked):
            return None, "unlocked_group"
        reason = dnslog_check(d, now)
        return (None, reason) if reason else (d, None)

    def _penalty_allowed(self, now):
        w = self.cfg.watch
        if w.mode != "enforce":
            raise Fail("not_enforcing", f"watch.mode is {w.mode!r}, not 'enforce'")
        if sum(1 for p in self.state.penalties if p["until"] > now) >= w.max_active_penalties:
            raise Fail("too_many_penalties", "too many active penalties")

    def op_penalize(self, req):
        source, domain = req.get("source"), req.get("domain")
        if source not in PENALTY_SOURCES:
            raise Fail("bad_request", "'source' must be 'title' or 'vision'")
        if domain is not None and not isinstance(domain, str):
            raise Fail("bad_request", "'domain' must be a string or null")
        f = {k: _str_field(req, k, 300) or "" for k in ("app", "title")}
        f.update({k: _str_field(req, k, 1000) or "" for k in ("description", "reason")})
        with self.lock:
            now = time.time()
            self._penalty_allowed(now)
            never, unlocked = set(self.cfg.never_block), self.unlocked_url_hosts()
        d = dropped = None
        if domain is not None and domain.strip():
            d, dropped = self._validate_domain(domain[:300], never, unlocked, now)
        with self.lock:
            now = time.time()
            self._penalty_allowed(now)
            pid = "p" + secrets.token_hex(4)
            while self.penalty(pid):
                pid = "p" + secrets.token_hex(4)
            until = now + self.cfg.watch.penalty_minutes * 60
            rec = {"id": pid, "since": now, "until": until, "domain": d, "app": f["app"],
                   "title": f["title"], "description": f["description"], "source": source,
                   "reason": f["reason"]}
            self.state.penalties.append(rec)
            ev = {"id": pid, "until": until, "domain": d}
            if dropped:
                ev["domain_dropped"] = dropped
            self.log("penalty", **ev, app=f["app"], title=f["title"],
                     description=f["description"], source=source, reason=f["reason"])
            self._save()
            self.reconcile()
            resp = {"ok": True, "penalty": dict(rec)}
            if dropped:
                resp["domain_dropped"] = dropped
            return resp


OPS = {"status", "unlock", "reply", "lock", "log", "observe", "penalize", "appeal"}


# ---------------------------------------------------------------- socket

def _read_request(sock) -> bytes:
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST:
            raise ValueError("request larger than 64 KB")
    line = data.split(b"\n", 1)[0]
    if len(line) > MAX_REQUEST:
        raise ValueError("request larger than 64 KB")
    return line


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        d, sock = self.server.focusd, self.request
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
        except OSError:
            uid = -1
        sock.settimeout(IO_TIMEOUT)
        try:
            raw = _read_request(sock)
        except (OSError, ValueError) as e:
            resp = _err("bad_request", str(e) or "could not read request")
        else:
            sock.settimeout(None)
            resp = d.dispatch(raw, uid)
        try:
            sock.settimeout(IO_TIMEOUT)
            sock.sendall(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


# ---------------------------------------------------------------- entry

def main(argv=None) -> int:
    if paths.root() == "" and os.geteuid() != 0:
        print("focusd: must run as root (set FOCUS_ROOT to run unprivileged for tests)",
              file=sys.stderr)
        raise SystemExit(1)
    rc = Daemon().run()
    if rc:
        raise SystemExit(rc)
    return 0


if __name__ == "__main__":
    main()
