"""Tests for focus/cli.py against a fake focusd socket server (§7, §9)."""

import copy
import io
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from focus import cli  # noqa: E402

# Thu 05 Nov 2026 17:33:20 UTC; the tests run with TZ=UTC.
NOW = 1793900000.0
DAY_START = 1793851200.0          # 04:00 that day
_OLD_TZ = None


def setUpModule():
    global _OLD_TZ
    _OLD_TZ = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()


def tearDownModule():
    if _OLD_TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _OLD_TZ
    time.tzset()


def group(gid, label, **kw):
    g = {"id": gid, "label": label, "enabled": True, "unlockable": True, "locked": True,
         "unlocked_until": None, "used_today_min": 0, "daily_minutes": 40, "max_minutes": 20,
         "cooldown_until": None, "webapp_classes": [], "hosts_count": 0,
         "url_block": [gid + ".com"], "url_allow": [], "title_patterns": []}
    g.update(kw)
    return g


def make_status(**kw):
    st = {
        "ok": True, "version": "0.1.0", "mode": "light", "now": NOW, "started": NOW - 10000,
        "day_start": DAY_START,
        "groups": [
            group("youtube", "YouTube", used_today_min=15,
                  webapp_classes=["^chrome-(www\\.)?youtube\\.com__"],
                  url_block=["youtube.com", "youtu.be"],
                  url_allow=["music.youtube.com", "youtube.com/embed"]),
            group("reddit", "Reddit", locked=False, unlocked_until=NOW + 751, used_today_min=20),
            group("x", "X", cooldown_until=NOW + 300),
            group("porn", "Porn", unlockable=False, hosts_count=71234, url_block=[]),
            group("news", "News", enabled=False),
        ],
        "unlocks": [{"group": "reddit", "label": "Reddit", "since": NOW - 449, "until": NOW + 751,
                     "minutes": 20, "reason": "mod queue"}],
        "lists": {"porn": {"entries": 71234, "updated": NOW - 100000}},
        "judge_calls_last_hour": 2,
        "penalties": [],
        "watch": {"mode": "observe", "title_interval_seconds": 60, "last_seen": NOW - 40,
                  "last": {"t": NOW - 40, "verdict": "working", "app": "foot",
                           "title": "~/code", "source": "title"},
                  "flags_today": 3},
    }
    st.update(kw)
    return st


PENALTY = {"id": "p1a2b3c4", "since": NOW - 13 * 60 - 20, "until": NOW + 1430,
           "domain": "news.ycombinator.com", "app": "Hacker News",
           "title": "Hacker News - Vivaldi", "description": "HN front page",
           "source": "title", "reason": "distraction"}


class FakeDaemon:
    """A focusd stand-in: one JSON request per connection, canned replies."""

    def __init__(self, path):
        self.path = path
        self.requests = []
        self.routes = {}         # op -> dict | list (consumed in order) | callable | bytes
        self.release = threading.Event()
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline(65536)
                try:
                    req = json.loads(line)
                except ValueError:
                    req = {"_raw": line}
                fake.requests.append(req)
                resp = fake.reply(req)
                if resp is None:
                    return
                if isinstance(resp, bytes):
                    self.wfile.write(resp)
                else:
                    self.wfile.write((json.dumps(resp) + "\n").encode())

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False

        self.server = Server(path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def reply(self, req):
        op = req.get("op")
        route = self.routes.get(op)
        if route is None:
            if op == "status":
                return make_status()
            return {"ok": False, "error": "unknown_op", "message": "no canned reply"}
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if callable(route):
            return route(req)
        return copy.deepcopy(route)

    def ops(self):
        return [r.get("op") for r in self.requests]

    def last(self, op):
        return [r for r in self.requests if r.get("op") == op][-1]

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class FakeIn(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


class FakeOut(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


class KeyboardInterruptIn(FakeIn):
    def readline(self, *a):
        raise KeyboardInterrupt


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="focus-cli-", dir="/tmp")
        self.sock = os.path.join(self.tmp, "focusd.sock")
        self.daemon = FakeDaemon(self.sock)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.daemon.close)

    def run_cli(self, *argv, stdin="", tty_in=False, tty_out=False, env=None, stdin_obj=None):
        environ = {"FOCUS_SOCKET": self.sock, "COLUMNS": "80"}
        environ.update(env or {})
        out, errs = FakeOut(tty_out), FakeOut(False)
        sin = stdin_obj if stdin_obj is not None else FakeIn(stdin, tty_in)
        with mock.patch.dict(os.environ, environ), \
                mock.patch.object(cli, "_now", lambda: NOW), \
                mock.patch.object(sys, "stdin", sin), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", errs):
            for k in ("NO_COLOR", "FOCUS_ROOT"):
                if k not in environ:
                    os.environ.pop(k, None)
            try:
                cli.main(list(argv))
                code = None
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), errs.getvalue()


# --------------------------------------------------------------------------


class TestSocketPath(unittest.TestCase):
    def test_default_override_and_root(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.socket_path(), "/run/focusd/focusd.sock")
        with mock.patch.dict(os.environ, {"FOCUS_ROOT": "/tmp/fr"}, clear=True):
            self.assertEqual(cli.socket_path(), "/tmp/fr/run/focusd/focusd.sock")
        with mock.patch.dict(os.environ, {"FOCUS_ROOT": "/tmp/fr", "FOCUS_SOCKET": "/x.sock"},
                             clear=True):
            self.assertEqual(cli.socket_path(), "/x.sock")


class TestFormatting(unittest.TestCase):
    def setUp(self):
        setUpModule()

    def test_time_helpers(self):
        self.assertEqual(cli.clock(NOW, NOW), "17:33")
        self.assertEqual(cli.clock(DAY_START + 86400, NOW), "tomorrow 04:00")
        self.assertEqual(cli.mmss(751), "12:31")
        self.assertEqual(cli.mmss(3723), "1:02:03")
        self.assertEqual(cli.mmss(-5), "0:00")
        self.assertEqual(cli.minutes_text(1430), "24 min")
        self.assertEqual(cli.minutes_text(10), "1 min")
        self.assertEqual(cli.ago(40), "40 s ago")
        self.assertEqual(cli.ago(14 * 60), "14 min ago")


class TestStatus(CliTestCase):
    def test_human_view(self):
        self.daemon.routes["status"] = make_status(penalties=[PENALTY])
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.daemon.ops(), ["status"])
        self.assertIn("Unlocked  Reddit  12:31 left · until 17:45", out)
        self.assertIn("Locked    YouTube, X, Porn", out)
        self.assertIn("Off       News", out)
        self.assertIn("Today     YouTube 15/40 min · Reddit 20/40 min", out)
        self.assertIn("Penalty   Hacker News  23:50 left · appeal: focus appeal", out)
        self.assertIn("Monitor   observe · 3 flags today", out)
        self.assertNotIn("\033[", out)

    def test_status_subcommand_same_as_bare(self):
        self.assertEqual(self.run_cli("status")[1], self.run_cli()[1])

    def test_monitor_not_reporting(self):
        st = make_status()
        st["watch"]["last_seen"] = NOW - 181
        self.daemon.routes["status"] = st
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Monitor   observe · not reporting", out)

    def test_monitor_off_not_shown(self):
        st = make_status()
        st["watch"]["mode"] = "off"
        self.daemon.routes["status"] = st
        self.assertNotIn("Monitor", self.run_cli()[1])

    def test_json_is_verbatim(self):
        raw = '{"ok": true,  "version":"0.1.0", "groups": [], "z": "\\u00e9"}'
        self.daemon.routes["status"] = (raw + "\n").encode()
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(out, raw + "\n")
        self.assertEqual(err, "")

    def test_json_shortcut(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_json_daemon_error_exit_1(self):
        self.daemon.routes["status"] = {"ok": False, "error": "forbidden", "message": "uid 1001"}
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"], "forbidden")

    def test_human_daemon_error(self):
        self.daemon.routes["status"] = {"ok": False, "error": "forbidden", "message": "uid 1001"}
        code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("forbidden", err)

    def test_colour_only_on_tty(self):
        code, out, _ = self.run_cli(tty_out=True)
        self.assertEqual(code, 0)
        self.assertIn("\033[", out)
        _, out, _ = self.run_cli(tty_out=True, env={"NO_COLOR": "1"})
        self.assertNotIn("\033[", out)


class TestUnreachable(CliTestCase):
    def setUp(self):
        super().setUp()
        self.daemon.close()
        os.unlink(self.sock)

    def test_status_json(self):
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 3)
        self.assertEqual(out, '{"ok":false,"error":"daemon_unreachable"}\n')
        self.assertIn("no socket", err)

    def test_human_commands_exit_3(self):
        for argv in (["status"], ["unlock", "youtube", "need", "it"], ["lock"], ["log"],
                     ["groups"], ["appeal", "it was work"], ["watch"]):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 3, argv)
            self.assertIn("can't reach focusd", err)
            self.assertEqual(out, "")

    def test_other_json_commands(self):
        for argv in (["log", "--json"], ["watch", "--json"]):
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(out), {"ok": False, "error": "daemon_unreachable"})

    def test_connection_refused(self):
        open(self.sock, "w").close()          # a dead socket path
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 3)
        self.assertIn("daemon_unreachable", out)


class TestBrokenDaemon(CliTestCase):
    def test_timeout(self):
        def hang(req):
            self.daemon.release.wait(5)
            return None
        self.daemon.routes["status"] = hang
        with mock.patch.object(cli, "REQUEST_TIMEOUT", 0.3):
            code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 3)
        self.assertIn("daemon_unreachable", out)
        self.assertIn("no answer", err)

    def test_garbage(self):
        self.daemon.routes["status"] = b"not json\n"
        code, _, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("invalid response", err)

    def test_closed_without_reply(self):
        self.daemon.routes["status"] = lambda req: None
        code, _, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("empty response", err)


GRANT = {"ok": True, "session": "ab12cd34ef56ab78", "decision": "grant",
         "message": "Fine, one lecture.", "granted_minutes": 15, "until": NOW + 900}
DENY = {"ok": True, "session": "ab12cd34ef56ab78", "decision": "deny",
        "message": "Watch it tonight."}
ASK = {"ok": True, "session": "ab12cd34ef56ab78", "decision": "ask",
       "message": "Which lecture, and why now?"}


class TestUnlock(CliTestCase):
    def test_grant_with_args(self):
        self.daemon.routes["unlock"] = GRANT
        code, out, err = self.run_cli("unlock", "youtube", "-m", "15", "lecture", "video")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.daemon.last("unlock"),
                         {"op": "unlock", "group": "youtube", "reason": "lecture video",
                          "minutes": 15})
        self.assertIn("Asking the judge…", out)
        self.assertIn("Judge: Fine, one lecture.", out)
        self.assertIn("YouTube unlocked for 15 min (until 17:48).", out)

    def test_minutes_anywhere_and_label_lookup(self):
        self.daemon.routes["unlock"] = GRANT
        code, _, _ = self.run_cli("unlock", "YouTube", "lecture", "-m", "10", "video")
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.last("unlock"),
                         {"op": "unlock", "group": "youtube", "reason": "lecture video",
                          "minutes": 10})

    def test_no_minutes_field_when_not_given(self):
        self.daemon.routes["unlock"] = GRANT
        self.run_cli("unlock", "youtube", "lecture video")
        self.assertNotIn("minutes", self.daemon.last("unlock"))

    def test_extend_wording(self):
        self.daemon.routes["unlock"] = dict(GRANT, until=NOW + 751 + 900)
        code, out, _ = self.run_cli("unlock", "reddit", "one more thread")
        self.assertEqual(code, 0)
        self.assertIn("Reddit extended by 15 min (until 18:00).", out)

    def test_deny(self):
        st_after = make_status()
        st_after["groups"][0]["cooldown_until"] = NOW + 300
        self.daemon.routes["status"] = [make_status(), st_after]
        self.daemon.routes["unlock"] = DENY
        code, out, _ = self.run_cli("unlock", "youtube", "just bored")
        self.assertEqual(code, 1)
        self.assertIn("Judge: Watch it tonight.", out)
        self.assertIn("Denied. You can ask again at 17:38.", out)

    def test_ask_loop(self):
        self.daemon.routes["unlock"] = ASK
        self.daemon.routes["reply"] = [dict(ASK, message="Is it due tomorrow?"), GRANT]
        code, out, err = self.run_cli("unlock", "youtube", "a lecture",
                                      stdin="Linear algebra, lecture 7\n\nyes\n", tty_in=True)
        self.assertEqual(code, 0, err)
        replies = [r for r in self.daemon.requests if r["op"] == "reply"]
        self.assertEqual(replies, [
            {"op": "reply", "session": "ab12cd34ef56ab78", "answer": "Linear algebra, lecture 7"},
            {"op": "reply", "session": "ab12cd34ef56ab78", "answer": "yes"},
        ])
        self.assertIn("Judge: Which lecture, and why now?", out)
        self.assertIn("Judge: Is it due tomorrow?", out)
        self.assertIn("(a few words, please", out)   # the empty answer was re-asked
        self.assertEqual(out.count("Asking the judge…"), 3)
        self.assertIn("YouTube unlocked for 15 min", out)

    def test_ask_without_tty_is_usage_error(self):
        self.daemon.routes["unlock"] = ASK
        code, out, err = self.run_cli("unlock", "youtube", "a lecture")
        self.assertEqual(code, 2)
        self.assertIn("Which lecture, and why now?", out)
        self.assertIn("not a terminal", err)
        self.assertNotIn("reply", self.daemon.ops())

    def test_refused_rate_limit(self):
        self.daemon.routes["unlock"] = {"ok": True, "session": "s1", "decision": "refused",
                                        "message": "rate limit", "rule": "rate_limit",
                                        "retry_after": NOW + 1200}
        code, out, _ = self.run_cli("unlock", "youtube", "lecture video")
        self.assertEqual(code, 1)
        self.assertIn("Too many judge calls in the last hour. Try again at 17:53.", out)

    def test_refused_budget_from_daemon(self):
        self.daemon.routes["unlock"] = {"ok": True, "decision": "refused", "message": "",
                                        "rule": "budget", "retry_after": DAY_START + 86400}
        code, out, _ = self.run_cli("unlock", "youtube", "lecture video")
        self.assertEqual(code, 1)
        self.assertIn("Not enough YouTube time left today. It resets at tomorrow 04:00.", out)

    def test_judge_error(self):
        self.daemon.routes["unlock"] = {"ok": True, "session": "s1", "decision": "error",
                                        "message": "judge timed out"}
        code, out, _ = self.run_cli("unlock", "youtube", "lecture video")
        self.assertEqual(code, 1)
        self.assertIn("No decision from the judge. Nothing changed", out)
        self.assertIn("  judge timed out", out)

    def test_daemon_error_response(self):
        self.daemon.routes["unlock"] = ASK
        self.daemon.routes["reply"] = {"ok": False, "error": "unknown_session",
                                       "message": "expired"}
        code, _, err = self.run_cli("unlock", "youtube", "lecture", stdin="lecture 7\n",
                                    tty_in=True)
        self.assertEqual(code, 1)
        self.assertIn("expired", err)

    def test_pick_group_and_prompt(self):
        self.daemon.routes["unlock"] = GRANT
        code, out, err = self.run_cli("unlock", stdin="5\n1\n\nlecture 7 for the exam\n",
                                      tty_in=True)
        self.assertEqual(code, 0, err)
        menu = out.split("Unlock which group?")[1].split("Why do you need")[0]
        self.assertIn("1) YouTube ", menu)
        self.assertIn("2) Reddit (unlocked, 13 min left — extend)", menu)
        self.assertNotIn("3)", menu)             # X is in cooldown: listed, not numbered
        self.assertRegex(menu, r"\n {5}X +cooldown until 17:38")
        for hidden in ("Porn", "News"):          # not unlockable / disabled
            self.assertNotIn(hidden, menu)
        self.assertIn("(type a number from the list", out)
        self.assertIn("Why do you need YouTube right now?", out)
        self.assertEqual(self.daemon.last("unlock")["reason"], "lecture 7 for the exam")
        self.assertEqual(self.daemon.last("unlock")["group"], "youtube")

    def test_pick_unlocked_group_extends(self):
        self.daemon.routes["unlock"] = dict(GRANT, until=NOW + 751 + 900)
        code, out, err = self.run_cli("unlock", stdin="2\none more thread\n", tty_in=True)
        self.assertEqual(code, 0, err)
        self.assertIn("Why do you need Reddit right now?", out)
        self.assertEqual(self.daemon.last("unlock")["group"], "reddit")
        self.assertIn("Reddit extended by 15 min (until 18:00).", out)

    def test_pick_by_name(self):
        self.daemon.routes["unlock"] = GRANT
        code, _, _ = self.run_cli("unlock", stdin="youtube\nlecture 7\n", tty_in=True)
        self.assertEqual(code, 0)

    def test_nothing_to_pick(self):
        st = make_status()
        st["groups"][0]["used_today_min"] = 40
        st["groups"][1]["used_today_min"] = 40
        self.daemon.routes["status"] = st
        code, out, _ = self.run_cli("unlock", tty_in=True)
        self.assertEqual(code, 1)
        self.assertIn("Nothing can be unlocked right now.", out)
        self.assertIn("No YouTube time left today (40 of 40 min used). It resets at tomorrow 04:00.",
                      out)

    def test_no_group_without_tty(self):
        code, _, err = self.run_cli("unlock")
        self.assertEqual(code, 2)
        self.assertIn("not a terminal", err)
        self.assertEqual(self.daemon.requests, [])

    def test_no_reason_without_tty(self):
        code, _, err = self.run_cli("unlock", "youtube")
        self.assertEqual(code, 2)
        self.assertEqual(self.daemon.requests, [])

    def test_precheck_cooldown(self):
        code, out, _ = self.run_cli("unlock", "x", "need it")
        self.assertEqual(code, 1)
        self.assertIn("X was denied recently. You can ask again at 17:38.", out)
        self.assertNotIn("unlock", self.daemon.ops())

    def test_precheck_not_unlockable_and_disabled(self):
        code, out, _ = self.run_cli("unlock", "porn", "please")
        self.assertEqual(code, 1)
        self.assertIn("Porn can't be unlocked.", out)
        code, out, _ = self.run_cli("unlock", "news", "please")
        self.assertEqual(code, 1)
        self.assertIn("News isn't blocked", out)
        self.assertNotIn("unlock", self.daemon.ops())

    def test_unknown_group(self):
        code, out, _ = self.run_cli("unlock", "youtub", "lecture")
        self.assertEqual(code, 1)
        self.assertIn("No group called 'youtub'. Groups: youtube, reddit, x, porn, news.", out)

    def test_bad_reason_and_minutes_are_usage_errors(self):
        self.assertEqual(self.run_cli("unlock", "youtube", "x")[0], 2)
        self.assertEqual(self.run_cli("unlock", "youtube", "y" * 2001)[0], 2)
        self.assertEqual(self.run_cli("unlock", "youtube", "-m", "0", "lecture")[0], 2)
        self.assertEqual(self.run_cli("unlock", "youtube", "-m", "ten", "lecture")[0], 2)
        self.assertNotIn("unlock", self.daemon.ops())

    def test_eof_at_prompt(self):
        code, out, _ = self.run_cli("unlock", "youtube", stdin="", tty_in=True)
        self.assertEqual(code, 1)
        self.assertIn("Cancelled.", out)
        self.assertNotIn("unlock", self.daemon.ops())

    def test_ctrl_c_at_prompt(self):
        code, _, _ = self.run_cli("unlock", "youtube",
                                  stdin_obj=KeyboardInterruptIn("", tty=True))
        self.assertEqual(code, 130)
        self.assertNotIn("unlock", self.daemon.ops())

    def test_ctrl_c_while_waiting(self):
        real = cli.request

        def fake(obj, timeout=None):
            if obj["op"] == "unlock":
                raise KeyboardInterrupt
            return real(obj, timeout)
        with mock.patch.object(cli, "request", fake):
            code, _, err = self.run_cli("unlock", "youtube", "lecture video")
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", err)

    def test_pause(self):
        self.daemon.routes["unlock"] = GRANT
        stdin = FakeIn("\n", tty=True)
        code, out, _ = self.run_cli("unlock", "--pause", "youtube", "lecture video",
                                    stdin_obj=stdin)
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("Press Enter to close."))
        self.assertEqual(stdin.read(), "")      # the Enter was consumed

    def test_tty_spinner_is_cleared(self):
        self.daemon.routes["unlock"] = GRANT
        code, out, _ = self.run_cli("unlock", "youtube", "lecture video", tty_out=True)
        self.assertEqual(code, 0)
        self.assertIn("\r\033[K", out)


class TestAppeal(CliTestCase):
    def setUp(self):
        super().setUp()
        older = dict(PENALTY, id="p00000001", since=NOW - 3000, app="Twitch")
        self.daemon.routes["status"] = make_status(penalties=[older, PENALTY])

    def test_latest_penalty_prompt_and_grant(self):
        self.daemon.routes["appeal"] = dict(GRANT, granted_minutes=None, until=None,
                                            message="Reading the HN thread on your library is fine.")
        code, out, err = self.run_cli("appeal", stdin="it was the thread about my bug\n",
                                      tty_in=True)
        self.assertEqual(code, 0, err)
        self.assertIn("Hacker News – caught 17:20, 24 min left", out)
        self.assertIn("  Hacker News - Vivaldi", out)
        self.assertIn("Why was this OK?", out)
        self.assertIn("Asking the judge…", out)
        self.assertIn("Penalty lifted.", out)
        self.assertEqual(self.daemon.last("appeal"),
                         {"op": "appeal", "penalty": "p1a2b3c4",
                          "reason": "it was the thread about my bug"})

    def test_reason_without_id(self):
        self.daemon.routes["appeal"] = DENY
        code, out, _ = self.run_cli("appeal", "it", "was", "work")
        self.assertEqual(code, 1)
        self.assertEqual(self.daemon.last("appeal")["penalty"], "p1a2b3c4")
        self.assertEqual(self.daemon.last("appeal")["reason"], "it was work")
        self.assertIn("Appeal denied. The penalty stays for 24 min more.", out)

    def test_explicit_id(self):
        self.daemon.routes["appeal"] = ASK
        self.daemon.routes["reply"] = GRANT
        code, out, _ = self.run_cli("appeal", "p00000001", "stream for a class",
                                    stdin="the lecturer streams on Twitch\n", tty_in=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.last("appeal")["penalty"], "p00000001")
        self.assertIn("Twitch – caught", out)
        self.assertEqual(self.daemon.last("reply")["answer"], "the lecturer streams on Twitch")

    def test_no_active_penalty(self):
        self.daemon.routes["status"] = make_status()
        code, out, _ = self.run_cli("appeal", tty_in=True)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "No active penalty.")

    def test_expired_penalty_ignored(self):
        self.daemon.routes["status"] = make_status(penalties=[dict(PENALTY, until=NOW - 1)])
        code, out, _ = self.run_cli("appeal", "it was work")
        self.assertEqual(code, 1)
        self.assertIn("No active penalty.", out)

    def test_unknown_id(self):
        code, out, _ = self.run_cli("appeal", "pdeadbeef", "it was work")
        self.assertEqual(code, 1)
        self.assertIn("No active penalty pdeadbeef.", out)

    def test_refused_cooldown(self):
        self.daemon.routes["appeal"] = {"ok": True, "decision": "refused", "rule": "cooldown",
                                        "message": "", "retry_after": NOW + 120}
        code, out, _ = self.run_cli("appeal", "it was work")
        self.assertEqual(code, 1)
        self.assertIn("This appeal was denied recently. You can ask again at 17:35.", out)

    def test_unknown_penalty_error(self):
        self.daemon.routes["appeal"] = {"ok": False, "error": "unknown_penalty", "message": ""}
        code, _, err = self.run_cli("appeal", "it was work")
        self.assertEqual(code, 1)
        self.assertIn("isn't active", err)

    def test_no_reason_without_tty(self):
        code, _, _ = self.run_cli("appeal")
        self.assertEqual(code, 2)
        self.assertEqual(self.daemon.requests, [])


class TestLock(CliTestCase):
    def test_default_all(self):
        self.daemon.routes["lock"] = {"ok": True, "locked": ["reddit", "youtube"]}
        code, out, _ = self.run_cli("lock")
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.last("lock"), {"op": "lock", "group": "all"})
        self.assertEqual(out.strip(), "Locked Reddit, YouTube.")

    def test_one_group_by_label(self):
        self.daemon.routes["lock"] = {"ok": True, "locked": ["reddit"]}
        code, out, _ = self.run_cli("lock", "Reddit")
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.last("lock"), {"op": "lock", "group": "reddit"})

    def test_already_locked(self):
        self.daemon.routes["lock"] = {"ok": True, "locked": []}
        self.assertEqual(self.run_cli("lock", "all")[1].strip(), "Everything was already locked.")
        self.assertEqual(self.daemon.last("lock")["group"], "all")
        self.assertEqual(self.run_cli("lock", "ALL")[1].strip(), "Everything was already locked.")
        self.assertEqual(self.daemon.last("lock")["group"], "all")
        self.assertEqual(self.run_cli("lock", "youtube")[1].strip(), "YouTube was already locked.")

    def test_unknown_group(self):
        code, out, _ = self.run_cli("lock", "nope")
        self.assertEqual(code, 1)
        self.assertNotIn("lock", self.daemon.ops())

    def test_error(self):
        self.daemon.routes["lock"] = {"ok": False, "error": "internal", "message": "boom"}
        code, _, err = self.run_cli("lock")
        self.assertEqual(code, 1)
        self.assertIn("internal error: boom", err)


EVENTS = [
    {"t": NOW - 86400 - 60, "type": "start", "version": "0.1.0"},
    {"t": NOW - 1000, "type": "request", "session": "s1", "group": "youtube",
     "reason": "lecture 7", "requested": 15},
    {"t": NOW - 990, "type": "verdict", "session": "s1", "group": "youtube",
     "decision": "grant", "minutes": 15, "message": "ok"},
    {"t": NOW - 990, "type": "unlock", "group": "youtube", "minutes": 15, "until": NOW - 90,
     "reason": "lecture 7"},
    {"t": NOW - 90, "type": "expire", "group": "youtube"},
    {"t": NOW - 80, "type": "restore", "file": "/etc/hosts"},
    {"t": NOW - 70, "type": "penalty", "id": "p1a2b3c4", "until": NOW + 1730, "domain": None,
     "domain_dropped": "no_dnslog", "app": "Hacker News", "title": "HN", "source": "title",
     "reason": "x"},
    {"t": NOW - 60, "type": "appeal_verdict", "session": "s2", "penalty": "p1a2b3c4",
     "decision": "grant", "message": "fine"},
    {"t": NOW - 50, "type": "penalty_end", "id": "p1a2b3c4", "by": "appeal"},
    {"t": NOW - 40, "type": "lock", "group": "all", "by": "user"},
    {"t": NOW - 30, "type": "something_new", "x": 1},
]


class TestLog(CliTestCase):
    def test_human_timeline(self):
        self.daemon.routes["log"] = {"ok": True, "events": EVENTS}
        code, out, err = self.run_cli("log")
        self.assertEqual(code, 0, err)
        req = self.daemon.last("log")
        self.assertEqual(req["limit"], 50)
        self.assertNotIn("since", req)
        self.assertNotIn("observe", req["types"])
        self.assertIn("penalty", req["types"])
        lines = out.splitlines()
        self.assertEqual(lines[0], "Wed 4 Nov")
        self.assertIn("  17:32  focusd started (0.1.0)", lines)
        self.assertIn("Thu 5 Nov", lines)
        self.assertIn('  17:16  asked for YouTube (15 min): "lecture 7"', lines)
        self.assertIn("  17:16  YouTube unlocked for 15 min, until 17:31", lines)
        self.assertIn("  17:31  YouTube locked again (time up)", lines)
        self.assertIn("  17:32  restored /etc/hosts after an outside edit", lines)
        self.assertIn("  17:32  penalty p1a2b3c4: Hacker News, blocked until 18:02 "
                      "(domain dropped: no_dnslog)", lines)
        self.assertIn("  17:32  penalty p1a2b3c4 ended (lifted on appeal)", lines)
        self.assertIn("  17:32  everything locked (by you)", lines)
        self.assertIn('  17:32  something_new {"x": 1}', lines)

    def test_options(self):
        self.daemon.routes["log"] = {"ok": True, "events": []}
        code, out, _ = self.run_cli("log", "--today", "--all", "-n", "5")
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.last("log"), {"op": "log", "limit": 5, "since": DAY_START})
        self.assertEqual(out.strip(), "Nothing logged today.")
        self.run_cli("log", "--today")
        self.assertEqual(self.daemon.last("log")["limit"], 500)

    def test_json_verbatim(self):
        raw = '{"ok":true,"events":[{"t":1,"type":"stop"}]}'
        self.daemon.routes["log"] = (raw + "\n").encode()
        code, out, _ = self.run_cli("log", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(out, raw + "\n")
        self.assertEqual(self.daemon.ops(), ["log"])

    def test_bad_n(self):
        self.assertEqual(self.run_cli("log", "-n", "0")[0], 2)
        self.assertEqual(self.run_cli("log", "-n", "501")[0], 2)

    def test_error(self):
        self.daemon.routes["log"] = {"ok": False, "error": "bad_request", "message": "limit"}
        self.assertEqual(self.run_cli("log")[0], 1)


class TestGroups(CliTestCase):
    def test_table(self):
        code, out, _ = self.run_cli("groups")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("youtube  YouTube  locked"))
        self.assertIn("15/40 min today · max 20 · youtube.com, youtu.be", lines[0])
        self.assertIn("unlocked  12:31 left", lines[1])
        self.assertIn("cooldown until 17:38", lines[2])
        self.assertIn("never unlockable · 71234 hosts", lines[3])
        self.assertIn("off", lines[4])


OBSERVE = [
    {"t": NOW - 400, "type": "observe", "verdict": "procrastinating", "source": "title",
     "app": "Hacker News", "class": "vivaldi-stable", "title": "Hacker News - Vivaldi",
     "description": "", "domain": "", "confidence": 1.0, "streak": 1},
    {"t": NOW - 40, "type": "observe", "verdict": "working", "source": "vision",
     "app": "arXiv", "class": "vivaldi-stable", "title": "2411.01234 - Vivaldi",
     "description": "a paper", "domain": "arxiv.org", "confidence": 0.93, "streak": 0},
]


class TestWatch(CliTestCase):
    def test_view(self):
        self.daemon.routes["status"] = make_status(penalties=[PENALTY])
        self.daemon.routes["log"] = {"ok": True, "events": OBSERVE}
        code, out, err = self.run_cli("watch")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.daemon.last("log"), {"op": "log", "limit": 10, "types": ["observe"]})
        self.assertIn("Monitor   observe · checks every 60 s", out)
        self.assertIn("Last      working · foot · 40 s ago", out)
        self.assertIn("Flags     3 today", out)
        self.assertIn("Penalties Hacker News  23:50 left (p1a2b3c4)", out)
        self.assertIn("Recent checks", out)
        self.assertIn("  17:26  procrastinating  Hacker News — Hacker News - Vivaldi", out)
        self.assertIn("  17:32  working          arXiv — 2411.01234 - Vivaldi (vision 0.93)", out)

    def test_not_reporting(self):
        st = make_status()
        st["watch"]["last_seen"] = None
        st["watch"]["last"] = None
        self.daemon.routes["status"] = st
        self.daemon.routes["log"] = {"ok": True, "events": []}
        code, out, _ = self.run_cli("watch", "-n", "3")
        self.assertEqual(code, 0)
        self.assertEqual(self.daemon.last("log")["limit"], 3)
        self.assertIn("monitor not reporting (no check since focusd started)", out)
        self.assertIn("Penalties none", out)
        st["watch"]["last_seen"] = NOW - 14 * 60
        self.daemon.routes["status"] = st
        self.assertIn("monitor not reporting (last check 14 min ago)", self.run_cli("watch")[1])

    def test_json(self):
        self.daemon.routes["status"] = make_status(penalties=[PENALTY])
        self.daemon.routes["log"] = {"ok": True, "events": OBSERVE}
        code, out, _ = self.run_cli("watch", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["watch"]["mode"], "observe")
        self.assertEqual([p["id"] for p in data["penalties"]], ["p1a2b3c4"])
        self.assertEqual(len(data["events"]), 2)


class TestUsage(CliTestCase):
    def test_unknown_command(self):
        code, _, err = self.run_cli("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)

    def test_bad_option(self):
        self.assertEqual(self.run_cli("status", "--bogus")[0], 2)

    def test_help(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("focus unlock", out)


if __name__ == "__main__":
    unittest.main()
