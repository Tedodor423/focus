"""`focus` command-line client (ARCHITECTURE.md §9).

Talks to focusd over its Unix socket (§7): one JSON request per connection,
one JSON reply, 150 s client timeout. Stdlib only; imports nothing from the
daemon modules.

Exit codes: 0 ok/grant, 1 deny/refused/error, 2 usage, 3 daemon unreachable,
130 Ctrl-C.
"""

import argparse
import datetime
import json
import math
import os
import re
import shutil
import socket
import sys
import textwrap
import threading
import time

DEFAULT_SOCKET = "/run/focusd/focusd.sock"
REQUEST_TIMEOUT = 150.0          # seconds; judge calls are slow (§7)
MAX_REQUEST = 64 * 1024          # the daemon rejects bigger requests
MAX_RESPONSE = 32 * 1024 * 1024  # sanity cap for log replies
REASON_MIN, REASON_MAX = 3, 2000
MAX_ROUNDS = 20                  # guard against a daemon that never stops asking
PENALTY_ID_RE = re.compile(r"^p[0-9a-f]{8}$")
UNREACHABLE_JSON = '{"ok":false,"error":"daemon_unreachable"}'

# Every event type in §8 except "observe"; `focus log` shows these by default
# (observe events are what `focus watch` is for, and there are many of them).
LOG_TYPES = [
    "start", "stop", "unclean_start", "config_reload", "config_error",
    "request", "followup", "verdict", "unlock", "refused", "expire", "lock",
    "restore", "hosts_error", "list_update", "list_error", "judge_error",
    "penalty", "penalty_end", "appeal", "appeal_verdict",
]

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_UNREACHABLE, EXIT_INTERRUPT = 0, 1, 2, 3, 130

USAGE = """\
usage: focus [command] [options]

  focus [status] [--json]               what is blocked right now
  focus unlock [group] [-m MIN] [reason…]
                                        ask the judge to unlock a group
  focus lock [group|all]                lock again now (default: all)
  focus log [-n N] [--today] [--all] [--json]
                                        timeline of recent events
  focus groups                          all groups and their budgets
  focus appeal [penalty-id] [reason…]   appeal a monitor penalty
  focus watch [-n N] [--json]           screen monitor state and recent checks

unlock and appeal also take --pause (wait for Enter before exiting, for
floating terminals).
"""


# --------------------------------------------------------------------------
# errors


class CliExit(Exception):
    """Stop the command with an exit code and an optional message."""

    def __init__(self, code, message=None, to_stderr=True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.to_stderr = to_stderr


class Unreachable(CliExit):
    def __init__(self, detail):
        super().__init__(EXIT_UNREACHABLE, "focus: can't reach focusd (%s)." % detail)
        self.detail = detail


class Cancelled(Exception):
    """End of input at a prompt."""


# --------------------------------------------------------------------------
# socket client


def socket_path():
    override = os.environ.get("FOCUS_SOCKET")
    if override:
        return override
    return os.environ.get("FOCUS_ROOT", "") + DEFAULT_SOCKET


def request_raw(obj, timeout=None):
    """Send one request; return (response dict, raw response line)."""
    if timeout is None:
        timeout = REQUEST_TIMEOUT
    data = (json.dumps(obj) + "\n").encode("utf-8")
    if len(data) > MAX_REQUEST:
        raise CliExit(EXIT_USAGE, "focus: request too large.")
    path = socket_path()
    deadline = time.monotonic() + timeout
    buf = bytearray()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(data)
        while b"\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            sock.settimeout(left)
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_RESPONSE:
                raise Unreachable("response too large")
    except FileNotFoundError:
        raise Unreachable("no socket at %s" % path) from None
    except ConnectionRefusedError:
        raise Unreachable("connection refused at %s" % path) from None
    except PermissionError:
        raise Unreachable("permission denied on %s" % path) from None
    except (TimeoutError, socket.timeout):
        raise Unreachable("no answer within %d s" % round(timeout)) from None
    except OSError as e:
        raise Unreachable("%s: %s" % (path, e.strerror or e)) from None
    finally:
        sock.close()
    line = bytes(buf).split(b"\n", 1)[0]
    if not line.strip():
        raise Unreachable("empty response")
    try:
        resp = json.loads(line)
    except ValueError:
        raise Unreachable("invalid response") from None
    if not isinstance(resp, dict) or "ok" not in resp:
        raise Unreachable("invalid response")
    return resp, line.decode("utf-8", "replace")


def request(obj, timeout=None):
    return request_raw(obj, timeout)[0]


def get_status():
    resp = request({"op": "status"})
    if not resp.get("ok"):
        raise CliExit(EXIT_FAIL, error_text(resp))
    return resp


ERROR_TEXT = {
    "forbidden": "focusd refused this user (forbidden).",
    "unknown_session": "This conversation with the judge expired. Start again.",
    "unknown_penalty": "That penalty isn't active any more.",
    "not_enforcing": "The monitor isn't in enforce mode.",
    "unknown_op": "focusd doesn't know this request (version mismatch?).",
}


def error_text(resp):
    code = resp.get("error") or "error"
    msg = resp.get("message")
    if code in ERROR_TEXT:
        return ERROR_TEXT[code] + ((" " + msg) if msg and code == "unknown_op" else "")
    if code == "internal":
        return "focusd hit an internal error%s." % ((": " + msg) if msg else "")
    return msg or code.replace("_", " ")


# --------------------------------------------------------------------------
# terminal helpers

_color = False
_readline_loaded = False

STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def _isatty(stream):
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _color_wanted():
    return (_isatty(sys.stdout) and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb")


def c(text, *styles):
    if not _color or not styles:
        return text
    return "\033[%sm%s\033[0m" % (";".join(STYLES[s] for s in styles), text)


def out(text=""):
    print(text, file=sys.stdout, flush=True)


def err(text):
    print(text, file=sys.stderr, flush=True)


def width():
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))


def one_line(text, limit=100):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def stdin_is_tty():
    return _isatty(sys.stdin)


def _enable_line_editing():
    global _readline_loaded
    if _readline_loaded:
        return
    _readline_loaded = True
    if sys.stdin is sys.__stdin__ and _isatty(sys.stdin) and _isatty(sys.stdout):
        try:
            import readline  # noqa: F401  (gives input() arrow keys and history)
        except Exception:
            pass


def read_line(prompt):
    """input() with clean Ctrl-C/Ctrl-D handling."""
    _enable_line_editing()
    try:
        return input(prompt)
    except EOFError:
        out()
        raise Cancelled() from None
    except KeyboardInterrupt:
        out()
        raise


def ask_text(question, min_len=REASON_MIN, max_len=REASON_MAX):
    """Prompt until we get text of an acceptable length."""
    if question:
        out(c(question, "bold"))
    while True:
        text = read_line(c("> ", "cyan")).strip()
        if len(text) < min_len:
            out(c("(a few words, please; Ctrl-C to cancel)", "dim"))
            continue
        if len(text) > max_len:
            out(c("(too long: at most %d characters)" % max_len, "dim"))
            continue
        return text


def require_tty(what):
    if not stdin_is_tty():
        raise CliExit(EXIT_USAGE, "focus: %s, and stdin is not a terminal to ask." % what)


class Waiting:
    """Shows "Asking the judge…" while a slow request runs."""

    def __init__(self, text):
        self.text = text
        self.tty = _isatty(sys.stdout)
        self.stop = threading.Event()
        self.thread = None

    def _spin(self):
        start = time.monotonic()
        while not self.stop.wait(1.0):
            secs = int(time.monotonic() - start)
            try:
                sys.stdout.write("\r%s %s" % (c(self.text, "dim"), c("%ds" % secs, "dim")))
                sys.stdout.flush()
            except (OSError, ValueError):
                return

    def __enter__(self):
        if self.tty:
            sys.stdout.write(c(self.text, "dim"))
            sys.stdout.flush()
            self.thread = threading.Thread(target=self._spin, daemon=True)
            self.thread.start()
        else:
            out(self.text)
        return self

    def __exit__(self, *exc):
        if self.thread:
            self.stop.set()
            self.thread.join()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        return False


# --------------------------------------------------------------------------
# time formatting


def _now():
    return time.time()


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def clock(t, now=None):
    """Local HH:MM, with 'tomorrow' or a date when it isn't today."""
    if now is None:
        now = _now()
    dt = datetime.datetime.fromtimestamp(t)
    today = datetime.datetime.fromtimestamp(now).date()
    hm = dt.strftime("%H:%M")
    if dt.date() == today:
        return hm
    if dt.date() == today + datetime.timedelta(days=1):
        return "tomorrow " + hm
    if dt.date() == today - datetime.timedelta(days=1):
        return "yesterday " + hm
    return "%s %d %s %s" % (dt.strftime("%a"), dt.day, dt.strftime("%b"), hm)


def mmss(seconds):
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def minutes_text(seconds):
    return "%d min" % max(1, math.ceil(max(0.0, seconds) / 60))


def ago(seconds):
    s = int(seconds)
    if s < 5:
        return "just now"
    if s < 60:
        return "%d s ago" % s
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%d h ago" % (s // 3600)
    return "%d d ago" % (s // 86400)


def day_header(t):
    dt = datetime.datetime.fromtimestamp(t)
    return "%s %d %s" % (dt.strftime("%a"), dt.day, dt.strftime("%b"))


def fmt_min(x):
    x = _num(x)
    return str(int(x)) if x == int(x) else "%.1f" % x


# --------------------------------------------------------------------------
# groups


def group_label(g):
    return g.get("label") or g.get("id") or "?"


def labels_of(status):
    return {g.get("id"): group_label(g) for g in (status or {}).get("groups") or []}


def find_group(groups, name):
    name = (name or "").strip()
    low = name.lower()
    for g in groups:
        if g.get("id") == name:
            return g
    for g in groups:
        if (g.get("id") or "").lower() == low or group_label(g).lower() == low:
            return g
    return None


def budget_left(g):
    if g.get("daily_minutes") is None:
        return None
    return _num(g.get("daily_minutes")) - _num(g.get("used_today_min"))


def unlock_problem(g, status, now):
    """Hard rules we can see in status (§6). Returns a sentence or None."""
    label = group_label(g)
    if not g.get("enabled", True):
        return "%s isn't blocked (disabled in the config)." % label
    if not g.get("unlockable", True):
        return "%s can't be unlocked." % label
    cd = g.get("cooldown_until")
    if cd and _num(cd) > now:
        return "%s was denied recently. You can ask again at %s." % (label, clock(_num(cd), now))
    left = budget_left(g)
    if left is not None and left < 1:
        msg = "No %s time left today (%s of %s min used)." % (
            label, fmt_min(g.get("used_today_min")), fmt_min(g.get("daily_minutes")))
        ds = status.get("day_start")
        if ds:
            msg += " It resets at %s." % clock(_num(ds) + 86400, now)
        return msg
    return None


def unknown_group_text(name, groups):
    ids = ", ".join(g.get("id") or "?" for g in groups)
    return "No group called '%s'.%s" % (name, (" Groups: %s." % ids) if ids else "")


# --------------------------------------------------------------------------
# judge conversation (unlock and appeal)


def show_judge(message):
    message = (message or "").strip()
    if not message:
        return
    prefix = "Judge: "
    if _isatty(sys.stdout):
        body = textwrap.fill(" ".join(message.split()), width=width(),
                             initial_indent=" " * len(prefix),
                             subsequent_indent=" " * len(prefix))[len(prefix):]
    else:
        body = message
    out(c(prefix, "bold", "cyan") + body)


def ask_judge(req):
    try:
        with Waiting("Asking the judge…"):
            return request(req)
    except KeyboardInterrupt:
        err("Cancelled. The judge may still decide; run `focus` to check.")
        raise


def refusal_text(resp, label, now):
    rule = resp.get("rule") or ""
    retry = resp.get("retry_after")
    at = clock(_num(retry), now) if retry else None
    if rule == "cooldown":
        text = "%s was denied recently." % label
        return text + (" You can ask again at %s." % at if at else "")
    if rule == "budget":
        text = "Not enough %s time left today." % label
        return text + (" It resets at %s." % at if at else "")
    if rule == "rate_limit":
        text = "Too many judge calls in the last hour."
        return text + (" Try again at %s." % at if at else "")
    if rule == "not_unlockable":
        return "%s can't be unlocked." % label
    if rule == "not_enabled":
        return "%s isn't blocked (disabled in the config)." % label
    if rule == "unknown_group":
        return "No such group."
    if rule == "unknown_penalty":
        return ERROR_TEXT["unknown_penalty"]
    text = resp.get("message") or ("Refused (%s)." % rule if rule else "Refused.")
    return text + (" Try again at %s." % at if at else "")


def judge_loop(req, label, on_grant, on_deny, retry_command):
    """Run the request / ask / reply loop. Returns the exit code."""
    rounds = 0
    while True:
        resp = ask_judge(req)
        if not resp.get("ok"):
            err("focus: " + error_text(resp))
            return EXIT_FAIL
        decision = resp.get("decision")
        message = resp.get("message") or ""
        if decision == "ask":
            show_judge(message or "(the judge asked something, but sent no text)")
            session = resp.get("session")
            if not session:
                err("focus: the judge asked a question but focusd sent no session id.")
                return EXIT_FAIL
            if not stdin_is_tty():
                err("focus: the judge has a question, but stdin is not a terminal. "
                    "Run `%s` in a terminal." % retry_command)
                return EXIT_USAGE
            rounds += 1
            if rounds > MAX_ROUNDS:
                err("focus: too many questions; giving up.")
                return EXIT_FAIL
            answer = ask_text(None, min_len=1)
            req = {"op": "reply", "session": session, "answer": answer}
            continue
        if decision == "grant":
            show_judge(message)
            on_grant(resp)
            return EXIT_OK
        if decision == "deny":
            show_judge(message)
            on_deny(resp)
            return EXIT_FAIL
        if decision == "refused":
            out(refusal_text(resp, label, _now()))
            return EXIT_FAIL
        if decision == "error":
            out(c("No decision from the judge. Nothing changed; try again in a moment.",
                  "yellow"))
            if message:
                out(c("  " + one_line(message, 200), "dim"))
            return EXIT_FAIL
        err("focus: unexpected answer from focusd (decision %r)." % decision)
        return EXIT_FAIL


# --------------------------------------------------------------------------
# commands


def cmd_status(args):
    if args.json:
        resp, raw = request_raw({"op": "status"})
        out(raw)
        return EXIT_OK if resp.get("ok") else EXIT_FAIL
    st = get_status()
    now = _num(st.get("now"), _now())
    groups = st.get("groups") or []
    rows = []

    unlocks = sorted((u for u in st.get("unlocks") or [] if _num(u.get("until")) > now),
                     key=lambda u: _num(u.get("until")))
    labels = labels_of(st)
    for i, u in enumerate(unlocks):
        label = u.get("label") or labels.get(u.get("group")) or u.get("group")
        rows.append(("Unlocked" if i == 0 else "",
                     "%s  %s left · until %s" % (c(label, "bold"),
                                                  mmss(_num(u.get("until")) - now),
                                                  clock(_num(u.get("until")), now)),
                     None))

    locked = [group_label(g) for g in groups if g.get("enabled", True) and g.get("locked")]
    rows.append(("Locked", ", ".join(locked) if locked else "nothing", None))

    off = [group_label(g) for g in groups if not g.get("enabled", True)]
    if off:
        rows.append(("Off", ", ".join(off), "dim"))

    used = ["%s %s/%s min" % (group_label(g), fmt_min(g.get("used_today_min")),
                              fmt_min(g.get("daily_minutes")))
            for g in groups if _num(g.get("used_today_min")) > 0]
    rows.append(("Today", " · ".join(used) if used else "no unlocks yet", None))

    pens = sorted((p for p in st.get("penalties") or [] if _num(p.get("until")) > now),
                  key=lambda p: _num(p.get("since")), reverse=True)
    for i, p in enumerate(pens):
        text = "%s  %s left" % (p.get("app") or p.get("domain") or p.get("id"),
                                mmss(_num(p.get("until")) - now))
        if i == 0:
            text += c(" · appeal: focus appeal", "dim")
        rows.append(("Penalty" if i == 0 else "", text, "red"))

    w = st.get("watch")
    if isinstance(w, dict) and w.get("mode") and w.get("mode") != "off":
        if monitor_silent(w, now):
            rows.append(("Monitor", "%s · not reporting" % w.get("mode"), "red"))
        else:
            flags = int(_num(w.get("flags_today")))
            rows.append(("Monitor", "%s · %d flag%s today" % (
                w.get("mode"), flags, "" if flags == 1 else "s"), None))

    print_rows(rows)
    return EXIT_OK


def print_rows(rows):
    for head, text, style in rows:
        head_s = c("%-10s" % head, "dim") if head else " " * 10
        out(head_s + (c(text, style) if style else text))


def monitor_silent(w, now):
    last = w.get("last_seen")
    interval = _num(w.get("title_interval_seconds"), 60.0) or 60.0
    return last is None or now - _num(last) > 3 * interval


def pick_group(groups, status, now):
    """Numbered list of unlockable groups (§9). Already-unlocked ones are
    offered too: the daemon extends them (§6)."""
    candidates = [g for g in groups if g.get("enabled", True) and g.get("unlockable", True)]
    ready, blocked = [], []
    for g in candidates:
        problem = unlock_problem(g, status, now)
        (blocked if problem else ready).append((g, problem))
    if not ready:
        for g, problem in blocked:
            out(c(problem, "dim"))
        raise CliExit(EXIT_FAIL, "Nothing can be unlocked right now.", to_stderr=False)

    def name(g):
        if g.get("locked", True):
            return group_label(g)
        left = _num(g.get("unlocked_until")) - now
        return "%s (unlocked, %s left — extend)" % (group_label(g), minutes_text(left))

    out(c("Unlock which group?", "bold"))
    name_w = max(len(name(g)) for g in candidates)
    for i, (g, _) in enumerate(ready, 1):
        left = budget_left(g)
        info = "%s min left today" % fmt_min(left) if left is not None else ""
        if g.get("max_minutes"):
            info += ("%sup to %s per unlock" % (" · " if info else "", fmt_min(g["max_minutes"])))
        out("  %d) %s  %s" % (i, name(g).ljust(name_w), c(info, "dim")))
    for g, problem in blocked:
        cd = g.get("cooldown_until")
        why = ("cooldown until %s" % clock(_num(cd), now)) if cd and _num(cd) > now \
            else "no time left today"
        out(c("     %s  %s" % (name(g).ljust(name_w), why), "dim"))

    while True:
        choice = read_line(c("> ", "cyan")).strip()
        if not choice:
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(ready):
            return ready[int(choice) - 1][0]
        g = find_group(groups, choice)
        if g is not None:
            for rg, _ in ready:
                if rg is g:
                    return g
            out(c(unlock_problem(g, status, now) or
                  "%s isn't locked." % group_label(g), "dim"))
            continue
        out(c("(type a number from the list; Ctrl-C to cancel)", "dim"))


def cmd_unlock(args):
    words = args.words
    group_arg = words[0] if words else None
    reason = " ".join(words[1:]).strip() or None
    if group_arg is None:
        require_tty("no group given")
    elif reason is None:
        require_tty("no reason given")
    if reason is not None and not REASON_MIN <= len(reason) <= REASON_MAX:
        raise CliExit(EXIT_USAGE, "focus: the reason must be %d–%d characters."
                      % (REASON_MIN, REASON_MAX))

    st = get_status()
    now = _now()
    groups = st.get("groups") or []
    if group_arg is None:
        g = pick_group(groups, st, now)
    else:
        g = find_group(groups, group_arg)
        if g is None:
            raise CliExit(EXIT_FAIL, unknown_group_text(group_arg, groups), to_stderr=False)
        problem = unlock_problem(g, st, now)
        if problem:
            raise CliExit(EXIT_FAIL, problem, to_stderr=False)
    label = group_label(g)
    gid = g.get("id")
    was_unlocked = g.get("enabled", True) and not g.get("locked", True)

    if reason is None:
        reason = ask_text("Why do you need %s right now?" % label)

    req = {"op": "unlock", "group": gid, "reason": reason}
    if args.minutes is not None:
        req["minutes"] = args.minutes

    def on_grant(resp):
        mins = resp.get("granted_minutes")
        until = resp.get("until")
        when = " (until %s)" % clock(_num(until)) if until else ""
        verb = "extended by" if was_unlocked else "unlocked for"
        out(c("%s %s %s min%s." % (label, verb, fmt_min(mins), when), "green", "bold"))

    def on_deny(resp):
        text = "Denied."
        try:
            st2 = request({"op": "status"}, timeout=5)
            g2 = find_group(st2.get("groups") or [], gid) if st2.get("ok") else None
            cd = g2 and g2.get("cooldown_until")
            if cd and _num(cd) > _now():
                text += " You can ask again at %s." % clock(_num(cd))
        except CliExit:
            pass
        out(c(text, "yellow"))

    return judge_loop(req, label, on_grant, on_deny, "focus unlock %s" % gid)


def active_penalties(st, now):
    return sorted((p for p in st.get("penalties") or [] if _num(p.get("until")) > now),
                  key=lambda p: _num(p.get("since")), reverse=True)


def cmd_appeal(args):
    words = list(args.words)
    pid = None
    if words and PENALTY_ID_RE.match(words[0]):
        pid = words.pop(0)
    reason = " ".join(words).strip() or None
    if reason is None:
        require_tty("no reason given")
    elif not REASON_MIN <= len(reason) <= REASON_MAX:
        raise CliExit(EXIT_USAGE, "focus: the reason must be %d–%d characters."
                      % (REASON_MIN, REASON_MAX))

    st = get_status()
    now = _now()
    pens = active_penalties(st, now)
    if pid is None:
        if not pens:
            raise CliExit(EXIT_FAIL, "No active penalty.", to_stderr=False)
        p = pens[0]
    else:
        p = next((x for x in pens if x.get("id") == pid), None)
        if p is None:
            raise CliExit(EXIT_FAIL, "No active penalty %s." % pid, to_stderr=False)
    pid = p.get("id")
    what = p.get("app") or p.get("domain") or "Something"

    out(c("%s – caught %s, %s left" % (what, clock(_num(p.get("since")), now),
                                        minutes_text(_num(p.get("until")) - now)), "bold"))
    title = one_line(p.get("title"), width() - 2)
    if title and title != what:
        out(c("  " + title, "dim"))

    if reason is None:
        reason = ask_text("Why was this OK?")

    def on_grant(resp):
        out(c("Penalty lifted.", "green", "bold"))
        out(c("Anything hidden comes back within a minute.", "dim"))

    def on_deny(resp):
        left = _num(p.get("until")) - _now()
        out(c("Appeal denied. The penalty stays for %s more." % minutes_text(left)
              if left > 0 else "Appeal denied.", "yellow"))

    req = {"op": "appeal", "penalty": pid, "reason": reason}
    return judge_loop(req, "This appeal", on_grant, on_deny, "focus appeal %s" % pid)


def cmd_lock(args):
    target = (args.group or "all").strip()
    labels = {}
    gid = "all" if target.lower() == "all" else target
    st = request({"op": "status"})        # labels and name lookup; unreachable → exit 3
    if st.get("ok"):
        labels = labels_of(st)
        if gid != "all":
            g = find_group(st.get("groups") or [], target)
            if g is None:
                raise CliExit(EXIT_FAIL, unknown_group_text(target, st.get("groups") or []),
                              to_stderr=False)
            gid = g.get("id")
    resp = request({"op": "lock", "group": gid})
    if not resp.get("ok"):
        err("focus: " + error_text(resp))
        return EXIT_FAIL
    locked = resp.get("locked") or []
    if locked:
        out("Locked %s." % ", ".join(labels.get(x, x) for x in locked))
    elif gid == "all":
        out("Everything was already locked.")
    else:
        out("%s was already locked." % labels.get(gid, gid))
    return EXIT_OK


def cmd_groups(args):
    st = get_status()
    now = _num(st.get("now"), _now())
    groups = st.get("groups") or []
    if not groups:
        out("No groups configured.")
        return EXIT_OK
    lw = max(len(group_label(g)) for g in groups)
    iw = max(len(g.get("id") or "") for g in groups)
    for g in groups:
        bits = []
        if not g.get("enabled", True):
            state, style = "off", "dim"
        elif not g.get("locked"):
            until = _num(g.get("unlocked_until"))
            state, style = "unlocked", "green"
            if until > now:
                bits.append("%s left" % mmss(until - now))
        else:
            state, style = "locked", None
        if g.get("enabled", True):
            if not g.get("unlockable", True):
                bits.append("never unlockable")
            else:
                cd = g.get("cooldown_until")
                if cd and _num(cd) > now:
                    bits.append("cooldown until %s" % clock(_num(cd), now))
                if g.get("daily_minutes") is not None:
                    bits.append("%s/%s min today" % (fmt_min(g.get("used_today_min")),
                                                     fmt_min(g.get("daily_minutes"))))
                if g.get("max_minutes"):
                    bits.append("max %s" % fmt_min(g.get("max_minutes")))
        blocks = list(g.get("url_block") or [])
        if _num(g.get("hosts_count")) and not blocks:
            n = int(_num(g.get("hosts_count")))
            blocks = ["%d host%s" % (n, "" if n == 1 else "s")]
        if blocks:
            shown = ", ".join(blocks[:3]) + (" +%d" % (len(blocks) - 3) if len(blocks) > 3 else "")
            bits.append(c(shown, "dim"))
        line = "%s  %s  %s  %s" % ((g.get("id") or "").ljust(iw), group_label(g).ljust(lw),
                                   c(state.ljust(8), style) if style else state.ljust(8),
                                   " · ".join(bits))
        out(line.rstrip())
    return EXIT_OK


def describe_event(e, labels, now):
    """One human line for an event: (text, style)."""
    t = e.get("type") or "?"

    def lab(gid):
        if gid == "all":
            return "everything"
        return labels.get(gid) or gid or "?"

    def q(text, limit=90):
        return '"%s"' % one_line(text, limit)

    if t == "start":
        return "focusd started%s" % (" (%s)" % e["version"] if e.get("version") else ""), None
    if t == "stop":
        return "focusd stopped", None
    if t == "unclean_start":
        return "focusd started after an unclean stop", "red"
    if t == "config_reload":
        return "config reloaded", None
    if t == "config_error":
        return "config problem: %s" % one_line(e.get("error")), "red"
    if t == "request":
        mins = e.get("requested")
        return "asked for %s%s: %s" % (lab(e.get("group")),
                                       " (%s min)" % fmt_min(mins) if mins else "",
                                       q(e.get("reason"))), None
    if t == "followup":
        return "answered: %s" % q(e.get("answer")), "dim"
    if t == "verdict":
        d = e.get("decision")
        if d == "grant":
            return "judge granted %s min: %s" % (fmt_min(e.get("minutes")), q(e.get("message"))), None
        if d == "deny":
            return "judge denied: %s" % q(e.get("message")), "yellow"
        if d == "ask":
            return "judge asked: %s" % q(e.get("message")), "dim"
        return "judge: %s %s" % (d, q(e.get("message"))), None
    if t == "unlock":
        until = e.get("until")
        return "%s unlocked for %s min%s" % (lab(e.get("group")), fmt_min(e.get("minutes")),
                                             ", until %s" % clock(_num(until), now) if until else ""), "green"
    if t == "refused":
        return "%s refused (%s)" % (lab(e.get("group")), e.get("rule")), "yellow"
    if t == "expire":
        return "%s locked again (time up)" % lab(e.get("group")), None
    if t == "lock":
        by = e.get("by")
        why = {"user": "by you", "shutdown": "focusd stopping"}.get(by, by)
        return "%s locked%s" % (lab(e.get("group")), " (%s)" % why if why else ""), None
    if t == "restore":
        return "restored %s after an outside edit" % e.get("file"), "red"
    if t == "hosts_error":
        return "hosts not written: %s" % one_line(e.get("error")), "red"
    if t == "list_update":
        return "list %s updated: %s entries" % (e.get("name"), "{:,}".format(int(_num(e.get("entries"))))), "dim"
    if t == "list_error":
        return "list %s failed: %s" % (e.get("name"), one_line(e.get("error"))), "yellow"
    if t == "judge_error":
        return "judge failed: %s" % one_line(e.get("error")), "red"
    if t == "observe":
        text = "seen %s: %s" % (e.get("verdict"), observe_text(e))
        return text, "yellow" if e.get("verdict") == "procrastinating" else "dim"
    if t == "penalty":
        extra = e.get("domain") or ("domain dropped: %s" % e["domain_dropped"]
                                    if e.get("domain_dropped") else "")
        until = e.get("until")
        return "penalty %s: %s%s%s" % (e.get("id"), e.get("app") or "?",
                                       ", blocked until %s" % clock(_num(until), now) if until else "",
                                       " (%s)" % extra if extra else ""), "red"
    if t == "penalty_end":
        by = {"expire": "time up", "appeal": "lifted on appeal"}.get(e.get("by"), e.get("by"))
        return "penalty %s ended (%s)" % (e.get("id"), by), "green" if e.get("by") == "appeal" else None
    if t == "appeal":
        return "appealed %s: %s" % (e.get("penalty"), q(e.get("reason"))), None
    if t == "appeal_verdict":
        d = e.get("decision")
        return "appeal %s: %s%s" % (e.get("penalty"), d,
                                    " — %s" % q(e.get("message")) if e.get("message") else ""), \
            "green" if d == "grant" else ("yellow" if d == "deny" else None)
    rest = {k: v for k, v in e.items() if k not in ("t", "type")}
    return "%s %s" % (t, one_line(json.dumps(rest, ensure_ascii=False), 90) if rest else ""), None


def observe_text(e):
    app = e.get("app") or e.get("class") or "?"
    title = one_line(e.get("title"), 70)
    text = app + (" — " + title if title and title != app else "")
    if e.get("source") == "vision":
        conf = e.get("confidence")
        text += " (vision%s)" % (" %.2f" % _num(conf) if conf is not None else "")
    return text


def print_timeline(events, labels, now, render=None):
    last_day = None
    for e in events:
        t = _num(e.get("t"))
        day = datetime.datetime.fromtimestamp(t).date()
        if day != last_day:
            out(c(day_header(t), "bold"))
            last_day = day
        text, style = (render or describe_event)(e, labels, now)
        out("  %s  %s" % (c(datetime.datetime.fromtimestamp(t).strftime("%H:%M"), "dim"),
                          c(text, style) if style else text))


def cmd_log(args):
    st = None
    if args.today or not args.json:
        st = get_status()
    req = {"op": "log", "limit": args.n if args.n is not None else (500 if args.today else 50)}
    if args.today:
        ds = st.get("day_start")
        if ds is None:
            d = datetime.datetime.fromtimestamp(_now()).replace(hour=0, minute=0, second=0,
                                                                 microsecond=0)
            ds = d.timestamp()
        req["since"] = ds
    if not args.all:
        req["types"] = LOG_TYPES
    resp, raw = request_raw(req)
    if args.json:
        out(raw)
        return EXIT_OK if resp.get("ok") else EXIT_FAIL
    if not resp.get("ok"):
        err("focus: " + error_text(resp))
        return EXIT_FAIL
    events = resp.get("events") or []
    if not events:
        out("Nothing logged%s." % (" today" if args.today else ""))
        return EXIT_OK
    print_timeline(events, labels_of(st), _now())
    return EXIT_OK


def render_observe(e, labels, now):
    v = e.get("verdict") or "?"
    style = {"procrastinating": "yellow", "working": None, "allowed": None}.get(v, "dim")
    return "%-15s  %s" % (v, observe_text(e)), style


def cmd_watch(args):
    st = get_status()
    log = request({"op": "log", "limit": args.n, "types": ["observe"]})
    if not log.get("ok"):
        if args.json:
            out(json.dumps(log))
        else:
            err("focus: " + error_text(log))
        return EXIT_FAIL
    events = log.get("events") or []
    now = _num(st.get("now"), _now())
    w = st.get("watch") if isinstance(st.get("watch"), dict) else {}
    pens = active_penalties(st, now)
    if args.json:
        out(json.dumps({"ok": True, "watch": w or None, "penalties": pens, "events": events},
                       ensure_ascii=False))
        return EXIT_OK

    mode = w.get("mode") or "unknown"
    rows = []
    interval = w.get("title_interval_seconds")
    rows.append(("Monitor", "%s%s" % (mode, " · checks every %s s" % fmt_min(interval)
                                      if interval and mode != "off" else ""), None))
    last = w.get("last") if isinstance(w.get("last"), dict) else None
    last_seen = w.get("last_seen")
    if mode != "off" or last_seen is not None:
        if monitor_silent(w, now):
            if last_seen is None:
                rows.append(("Last", "monitor not reporting (no check since focusd started)", "red"))
            else:
                rows.append(("Last", "monitor not reporting (last check %s)"
                             % ago(now - _num(last_seen)), "red"))
        else:
            v = (last or {}).get("verdict") or "?"
            app = (last or {}).get("app") or "?"
            rows.append(("Last", "%s · %s · %s" % (c(v, "yellow") if v == "procrastinating" else v,
                                                   app, ago(now - _num(last_seen))), None))
    flags = int(_num(w.get("flags_today")))
    rows.append(("Flags", "%d today" % flags, None))
    if pens:
        for i, p in enumerate(pens):
            rows.append(("Penalties" if i == 0 else "",
                         "%s  %s left (%s)" % (p.get("app") or p.get("domain") or "?",
                                               mmss(_num(p.get("until")) - now), p.get("id")), "red"))
    else:
        rows.append(("Penalties", "none", None))
    print_rows(rows)
    out()
    if not events:
        out(c("No checks logged yet.", "dim"))
        return EXIT_OK
    out(c("Recent checks", "bold"))
    print_timeline(events, labels_of(st), now, render=render_observe)
    return EXIT_OK


# --------------------------------------------------------------------------
# argument parsing


def _positive_int(text):
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a whole number: %r" % text) from None
    if v < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return v


def _log_limit(text):
    v = _positive_int(text)
    if v > 500:
        raise argparse.ArgumentTypeError("at most 500")
    return v


def build_parser(cmd):
    p = argparse.ArgumentParser(prog="focus " + cmd)
    if cmd == "status":
        p.add_argument("--json", action="store_true", help="print the daemon's status JSON")
    elif cmd == "unlock":
        p.add_argument("-m", "--minutes", type=_positive_int, help="minutes to ask for")
        p.add_argument("--pause", action="store_true", help="wait for Enter before exiting")
        p.add_argument("words", nargs="*", metavar="group [reason…]")
    elif cmd == "appeal":
        p.add_argument("--pause", action="store_true", help="wait for Enter before exiting")
        p.add_argument("words", nargs="*", metavar="[penalty-id] [reason…]")
    elif cmd == "lock":
        p.add_argument("group", nargs="?", help="group id or 'all' (default)")
    elif cmd == "log":
        p.add_argument("-n", type=_log_limit, help="number of events (default 50)")
        p.add_argument("--today", action="store_true", help="only events since the day started")
        p.add_argument("--all", action="store_true", help="include monitor checks")
        p.add_argument("--json", action="store_true", help="print the daemon's JSON")
    elif cmd == "watch":
        p.add_argument("-n", type=_log_limit, default=10, help="recent checks (default 10)")
        p.add_argument("--json", action="store_true", help="print JSON")
    return p


COMMANDS = {
    "status": cmd_status,
    "unlock": cmd_unlock,
    "lock": cmd_lock,
    "log": cmd_log,
    "groups": cmd_groups,
    "appeal": cmd_appeal,
    "watch": cmd_watch,
}


def pause():
    if not stdin_is_tty():
        return
    try:
        input(c("Press Enter to close.", "dim"))
    except (EOFError, KeyboardInterrupt):
        pass


def run(argv=None):
    global _color
    _color = _color_wanted()
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    if not argv:
        cmd, rest = "status", []
    elif argv[0] in ("-h", "--help", "help"):
        out(USAGE.rstrip())
        return EXIT_OK
    elif argv[0].startswith("-"):
        cmd, rest = "status", argv
    else:
        cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        err("focus: unknown command '%s'.\n\n%s" % (cmd, USAGE.rstrip()))
        return EXIT_USAGE
    try:
        args = build_parser(cmd).parse_intermixed_args(rest)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    try:
        code = COMMANDS[cmd](args)
    except Unreachable as e:
        if getattr(args, "json", False):
            out(UNREACHABLE_JSON)
        err(e.message)
        code = e.code
    except CliExit as e:
        if e.message:
            (err if e.to_stderr else out)(e.message)
        code = e.code
    except Cancelled:
        out("Cancelled.")
        code = EXIT_FAIL
    if getattr(args, "pause", False):
        pause()
    return code


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        code = run(argv)
    except KeyboardInterrupt:
        code = EXIT_INTERRUPT
    except BrokenPipeError:
        # `focus log | head`: stop quietly.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        code = EXIT_OK
    sys.exit(code)


if __name__ == "__main__":
    main()
