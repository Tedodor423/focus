"""focusd as a process: start/stop (fail closed), unclean start, config
errors and reloads, the peer-credential check and protocol errors."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes"))
import daemon_harness as H  # noqa: E402

GRANT = {"decision": "grant", "minutes": 10, "message": "ok"}
REASON = "reading docs for the project"


class Base(unittest.TestCase):
    def daemon(self, *responses, start=True, **kw):
        d = H.Daemon(responses=list(responses) or None, **kw)
        self.addCleanup(d.cleanup)
        return d.start() if start else d

    def groups(self, d):
        return [g["id"] for g in d.request({"op": "status"})["groups"]]


class Lifecycle(Base):
    def test_sigterm_fails_closed(self):
        d = self.daemon(GRANT)
        locked_hosts = d.hosts()
        locked_policy = d.policy()
        d.request({"op": "unlock", "group": "reddit", "reason": REASON})
        d.request({"op": "unlock", "group": "youtube", "reason": REASON})
        self.assertNotIn(b"reddit", d.hosts())
        self.assertEqual(d.stop(signal.SIGTERM), 0)
        self.assertEqual(d.hosts(), locked_hosts)
        self.assertEqual(d.policy(), locked_policy)
        self.assertEqual(d.policy("vivaldi"), locked_policy)
        self.assertEqual(d.state()["unlocks"], {})
        evs = d.events()
        self.assertEqual(evs[-1]["type"], "stop")
        self.assertEqual(sorted(e["group"] for e in evs if e["type"] == "lock"),
                         ["reddit", "youtube"])
        self.assertTrue(all(e["by"] == "shutdown" for e in evs if e["type"] == "lock"))
        self.assertFalse(os.path.exists(d.sock))

    def test_sigint_also_stops_cleanly(self):
        d = self.daemon()
        self.assertEqual(d.stop(signal.SIGINT), 0)
        self.assertEqual(d.events()[-1]["type"], "stop")
        d.start()
        self.assertEqual(d.events("unclean_start"), [])
        self.assertEqual(len(d.events("start")), 2)
        self.assertEqual(d.events("start")[0]["version"], "0.1.0")

    def test_unclean_start_keeps_unexpired_unlocks(self):
        d = self.daemon(start=False)
        now = time.time()
        d.write_events([{"t": now - 100, "type": "start", "version": "0.1.0"}])
        with open(d.path("/var/lib/focus/events.jsonl"), "a") as f:
            f.write('{"t": 1, "type": "tor')                  # torn last line
        d.write_state({"unlocks": {
            "reddit": {"since": now - 60, "until": now + 600, "minutes": 10, "reason": "r"},
            "youtube": {"since": now - 900, "until": now - 1, "minutes": 10, "reason": "r"}}})
        d.start()
        d.wait_event("unclean_start")
        status = d.request({"op": "status"})
        self.assertEqual([u["group"] for u in status["unlocks"]], ["reddit"])
        self.assertEqual(d.events("expire")[0]["group"], "youtube")
        self.assertNotIn(b"reddit", d.hosts())
        # The torn line was terminated, so new events stay parseable.
        with open(d.path("/var/lib/focus/events.jsonl")) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[1], '{"t": 1, "type": "tor')
        self.assertEqual(json.loads(lines[2])["type"], "start")

    def test_garbage_state_and_list_cache_do_not_crash(self):
        d = self.daemon(start=False)
        now = time.time()
        d.write_state({"unlocks": {"reddit": {"since": now, "until": now + 600,
                                              "minutes": "lots", "reason": 7}},
                       "penalties": [{"id": "pbad", "since": now, "until": now + 600,
                                      "domain": 5}, "junk"], "cooldowns": []})
        os.makedirs(d.path("/var/lib/focus/lists/porn.txt"))     # a dir: unreadable cache
        d.start()
        st = d.request({"op": "status"})
        self.assertEqual(st["unlocks"][0]["minutes"], 0)
        self.assertEqual([p["id"] for p in st["penalties"]], ["pbad"])
        self.assertIn(b"0.0.0.0 poki.com", d.hosts())

    def test_second_instance_refuses(self):
        d = self.daemon()
        other = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {H.REPO!r}); "
             "from focus.daemon import main; main()"],
            env={"FOCUS_ROOT": d.root, "PATH": "/usr/bin:/bin"}, capture_output=True,
            timeout=20)
        self.assertEqual(other.returncode, 1)
        self.assertIn(b"another focusd", other.stderr)
        self.assertTrue(d.request({"op": "status"})["ok"])
        self.assertEqual(len(d.events("start")), 1)

    def test_refuses_unprivileged_without_focus_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        env = {k: v for k, v in os.environ.items() if k != "FOCUS_ROOT"}
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {H.REPO!r}); "
                            "from focus.daemon import main; main()"],
                           env=env, capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"must run as root", r.stderr)


class ConfigHandling(Base):
    def test_bad_entries_are_dropped_and_logged(self):
        d = self.daemon(start=False)
        text = d.config_text().replace('hosts = ["poki.com"]',
                                       'hosts = ["poki.com", "10.0.0.1", "localhost"]')
        text += '\n[groups."Bad!"]\nhosts = ["x.com"]\n'
        text = text.replace("title_patterns = ['(?i) - YouTube( - [^-]+)?$']",
                            "title_patterns = ['(?i) - YouTube( - [^-]+)?$', '(broken']")
        d.write_config(text)
        d.start()
        errors = " | ".join(e["error"] for e in d.events("config_error"))
        for needle in ("10.0.0.1", "localhost", "Bad!", "(broken"):
            self.assertIn(needle, errors)
        self.assertEqual(self.groups(d), ["youtube", "reddit", "games", "porn", "off"])
        yt = d.request({"op": "status"})["groups"][0]
        self.assertEqual(yt["title_patterns"], ["(?i) - YouTube( - [^-]+)?$"])
        self.assertIn(b"0.0.0.0 poki.com", d.hosts())
        self.assertNotIn(b"10.0.0.1\n", d.hosts().split(b"# BEGIN")[1])

    def test_reload_keeps_old_config_on_errors(self):
        d = self.daemon()
        d.write_config("user = [broken toml")
        d.send(signal.SIGHUP)
        d.wait_event("config_error")
        self.assertEqual(len(self.groups(d)), 5)
        d.write_config(d.config_text().replace('mode = "light"', 'mode = "strict"'))
        d.send(signal.SIGHUP)
        d.wait_for(lambda: len(d.events("config_error")) >= 2)
        self.assertIn("light", d.events("config_error")[-1]["error"])
        self.assertEqual(d.events("config_reload"), [])
        self.assertEqual(d.request({"op": "status"})["mode"], "light")
        self.assertIn(b"0.0.0.0 reddit.com", d.hosts())

    def test_reload_on_mtime_change(self):
        d = self.daemon()
        time.sleep(0.05)
        d.write_config(d.config_text(watch_mode="nudge").replace(
            '[groups.reddit]\nlabel = "Reddit"', '[groups.reddit]\nlabel = "Reddit"\nenabled = false'))
        d.wait_event("config_reload", timeout=4)
        st = d.request({"op": "status"})
        self.assertEqual(st["watch"]["mode"], "nudge")
        reddit = next(g for g in st["groups"] if g["id"] == "reddit")
        self.assertEqual((reddit["enabled"], reddit["locked"]), (False, False))
        self.assertNotIn(b"reddit", d.hosts())
        self.assertNotIn("reddit.com", d.policy()["URLBlocklist"])

    def test_no_config_exits_nonzero(self):
        d = self.daemon(start=False)
        os.unlink(d.path("/etc/focus/config.toml"))
        d.start(wait=False)
        self.assertEqual(d.proc.wait(10), 1)
        self.assertIn("no usable config", d.stderr())
        self.assertEqual(d.hosts(), H.HOSTS)

    def test_first_start_with_unsupported_mode_still_locks(self):
        d = self.daemon(start=False)
        d.write_config(d.config_text().replace('mode = "light"', 'mode = "strict"'))
        d.start()
        self.assertIn("light", d.events("config_error")[0]["error"])
        self.assertIn(b"0.0.0.0 reddit.com", d.hosts())


class Health(Base):
    def test_hosts_policy_and_config_problems(self):
        begin = "# BEGIN focus — managed by focusd, manual edits are reverted\n".encode()
        d = self.daemon(hosts=H.HOSTS + begin + b"# END focus\n" + begin + b"# END focus\n")
        msg1 = "more than one '# BEGIN focus' marker"
        self.assertEqual(d.request({"op": "status"})["health"],
                         [f"hosts: refusing to write ({msg1})"])
        # A different problem is logged again (and shown), the same one is not.
        with open(d.path("/etc/hosts"), "wb") as f:
            f.write(H.HOSTS + begin)
        d.send(signal.SIGHUP)
        d.wait_for(lambda: len(d.events("hosts_error")) == 2)
        msg2 = "'# BEGIN focus' without '# END focus'"
        self.assertEqual(d.events("hosts_error")[-1]["error"], msg2)
        self.assertEqual(d.request({"op": "status"})["health"],
                         [f"hosts: refusing to write ({msg2})"])
        self.assertIn("hosts_error", d.stderr())
        # Fixed by hand → written and healthy again.
        with open(d.path("/etc/hosts"), "wb") as f:
            f.write(H.HOSTS)
        d.send(signal.SIGHUP)
        d.wait_for(lambda: b"# BEGIN" in d.hosts())
        self.assertEqual(d.request({"op": "status"})["health"], [])

        # Policy dir not writable.
        pdir = d.path("/etc/chromium/policies/managed")
        with open(os.path.join(pdir, "focus.json"), "w") as f:
            f.write("{}")
        os.chmod(pdir, 0o555)
        self.addCleanup(os.chmod, pdir, 0o755)
        d.send(signal.SIGHUP)
        d.wait_for(lambda: d.request({"op": "status"})["health"])
        health = d.request({"op": "status"})["health"]
        self.assertEqual(len(health), 1)
        self.assertTrue(health[0].startswith(f"policy: cannot write {pdir}/focus.json: "))
        os.chmod(pdir, 0o755)
        d.send(signal.SIGHUP)
        d.wait_for(lambda: d.request({"op": "status"})["health"] == [])
        self.assertIn("reddit.com", d.policy()["URLBlocklist"])

        # Broken config on reload → health until a good reload.
        with open(d.path("/etc/focus/config.toml")) as f:
            good = f.read()
        d.write_config("user = [broken")
        d.send(signal.SIGHUP)
        d.wait_for(lambda: d.request({"op": "status"})["health"])
        self.assertEqual(d.request({"op": "status"})["health"],
                         ["config: last reload failed, using previous config"])
        d.write_config(good)
        d.send(signal.SIGHUP)
        d.wait_for(lambda: d.request({"op": "status"})["health"] == [])

    def test_policy_file_rejection_in_health(self):
        d = self.daemon(start=False)
        real = d.path("/etc/focus/real-judge.md")
        os.replace(d.path("/etc/focus/judge.md"), real)
        os.symlink(real, d.path("/etc/focus/judge.md"))
        d.start()                                   # checked at start, no judge call needed
        health = d.request({"op": "status"})["health"]
        self.assertEqual(len(health), 1)
        self.assertTrue(health[0].startswith("judge: policy file rejected: "))
        self.assertIn("symlink", health[0])
        os.unlink(d.path("/etc/focus/judge.md"))     # missing is rejected too
        d.wait_for(lambda: "missing" in " ".join(d.request({"op": "status"})["health"]))
        os.replace(real, d.path("/etc/focus/judge.md"))   # a regular file again → healthy
        d.wait_for(lambda: d.request({"op": "status"})["health"] == [])

    def test_state_file_is_0600(self):
        d = self.daemon(start=False)
        d.write_state({"cooldowns": {}})
        st = d.path("/var/lib/focus/state.json")
        os.chmod(st, 0o644)
        d.start()
        self.assertEqual(os.stat(st).st_mode & 0o777, 0o600)
        d.request({"op": "penalize", "app": "a", "title": "t", "description": "",
                   "source": "title", "domain": None, "reason": ""})
        self.assertEqual(os.stat(st).st_mode & 0o777, 0o600)

    def test_events_file_is_0640(self):
        d = self.daemon(start=False)
        d.write_events([{"t": time.time() - 10, "type": "stop"}])
        ev = d.path("/var/lib/focus/events.jsonl")
        os.chmod(ev, 0o644)
        d.start()
        self.assertEqual(os.stat(ev).st_mode & 0o777, 0o640)
        d2 = self.daemon()
        self.assertEqual(os.stat(d2.path("/var/lib/focus/events.jsonl")).st_mode & 0o777, 0o640)


class Protocol(Base):
    def test_forbidden_peer(self):
        d = self.daemon(user="nobody", start=False)   # our uid is not allowed
        d.start(wait=False)
        H.wait_for(lambda: d.events("start"), 10)
        r = json.loads(d.raw(b'{"op":"status"}\n'))
        self.assertEqual((r["ok"], r["error"]), (False, "forbidden"))

    def test_allowed_peer(self):
        d = self.daemon()
        self.assertTrue(d.request({"op": "status"})["ok"])

    def test_bad_requests(self):
        d = self.daemon()
        cases = [(b"not json\n", "bad_request"), (b"[1,2]\n", "bad_request"),
                 (b'{"op":"explode"}\n', "unknown_op"), (b'{"no":"op"}\n', "unknown_op"),
                 (b'{"op":"status"}', None),                          # EOF instead of \n
                 (b'{"op":"log","pad":"' + b"x" * 70000 + b'"}\n', "bad_request")]
        for raw, code in cases:
            r = json.loads(d.raw(raw, eof=True))
            if code is None:
                self.assertTrue(r["ok"])
            else:
                self.assertEqual((r["ok"], r["error"]), (False, code), raw[:40])
                self.assertIn("message", r)
        self.assertTrue(d.request({"op": "status"})["ok"])   # still alive

    def test_status_shape(self):
        d = self.daemon()
        st = d.request({"op": "status"})
        self.assertEqual(set(st), {"ok", "version", "mode", "now", "started", "day_start",
                                   "groups", "unlocks", "lists", "judge_calls_last_hour",
                                   "penalties", "health", "watch"})
        self.assertEqual(st["health"], [])
        self.assertEqual(set(st["groups"][0]), {
            "id", "label", "enabled", "unlockable", "locked", "unlocked_until",
            "used_today_min", "daily_minutes", "max_minutes", "cooldown_until",
            "webapp_classes", "hosts_count", "url_block", "url_allow", "title_patterns"})
        self.assertLessEqual(st["day_start"], st["now"])
        self.assertLess(st["now"] - st["day_start"], 86400 + 3600)


if __name__ == "__main__":
    unittest.main()
