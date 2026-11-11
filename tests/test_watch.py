"""Unit tests for focus-watch (focus/watch.py), run in-process against fakes.

No real windows, processes, screenshots or notifications are touched: hyprctl, grim and
notify-send are fake scripts, focusd and Ollama are fake servers, and the only processes
signalled are `sleep` processes these tests start themselves.
"""
import base64
import io
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))
import watch_harness as H  # noqa: E402

W = H.load_watch_module()

YT_TITLE = "Some video - YouTube - Vivaldi"
YT_WEBAPP = "chrome-www.youtube.com__-Default"


class Base(unittest.TestCase):
    mode = "observe"
    watch_overrides: dict = {}

    def setUp(self):
        self.env = H.WatchEnv(mode=self.mode, watch=dict(self.watch_overrides))
        self.addCleanup(self.env.close)
        p = mock.patch.dict(os.environ, self.env.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("FOCUS_ROOT", None)
        p2 = mock.patch.object(W, "_hyprlock_running", return_value=False)
        p2.start()
        self.addCleanup(p2.stop)
        self.devnull = open(os.devnull, "w")
        self.addCleanup(self.devnull.close)
        p3 = mock.patch.object(sys, "stderr", self.devnull)  # keep the monitor's log lines out of test output
        p3.start()
        self.addCleanup(p3.stop)

    def watcher(self, **kw):
        w = W.Watcher(**kw)
        self.assertTrue(w.initial_config())
        w.state.load()
        return w

    def tick(self, w=None, **kw):
        w = w or self.watcher(**kw)
        return w, w.tick()

    def observes(self):
        return self.env.daemon.ops("observe")


# ============================================================================ pure helpers

class HelperTests(unittest.TestCase):
    def test_loopback_urls(self):
        ok = ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434/", "https://127.0.0.2",
              "http://127.0.0.1"]
        bad = ["http://192.168.1.5:11434", "http://example.com", "http://127.0.0.1.evil.com:11434",
               "http://127.0.0.1@evil.com/", "ftp://127.0.0.1", "http://0.0.0.0:11434", "http://[::]:11434",
               "http://user:pw@127.0.0.1", "http://127.0.0.1:99999", "", None, "127.0.0.1:11434",
               "http://10.0.0.1", "http://localhost.evil.com"]
        for u in ok:
            self.assertTrue(W.is_loopback_url(u), u)
        for u in bad:
            self.assertFalse(W.is_loopback_url(u), u)

    def test_normalize_domain(self):
        cases = {
            "news.ycombinator.com": "news.ycombinator.com",
            "https://www.News.YCombinator.com/item?id=1": "news.ycombinator.com",
            "www.reddit.com/r/foo": "reddit.com",
            "youtube.com:443": "youtube.com",
            " arxiv.org ": "arxiv.org",
            "": "", "Hacker News": "", "localhost": "", "127.0.0.1": "", "not a domain.com": "not",
            "a..b.com": "", "-bad.com": "", None: "", 42: "",
        }
        for raw, want in cases.items():
            got = W.normalize_domain(raw)
            if raw == "not a domain.com":
                self.assertEqual(got, "", raw)  # 'not' has no TLD
            else:
                self.assertEqual(got, want, raw)

    def test_move_expr_and_quoting(self):
        self.assertEqual(
            W.move_expr("0x557f9543cd90", "special:focusjail"),
            'hl.dsp.window.move({ workspace = "special:focusjail", follow = false, window = "address:0x557f9543cd90" })')
        self.assertEqual(W.lua_str('a"b\\c\n'), '"a\\034b\\092c\\010"')
        for bad in ("0x12; os.exit()", "", None, "address:0x1", "0x"):
            with self.assertRaises(ValueError):
                W.move_expr(bad, "1")

    def test_workspace_selector(self):
        self.assertEqual(W.workspace_selector(3, "3"), "3")
        self.assertEqual(W.workspace_selector(1, "Main"), "1")
        self.assertEqual(W.workspace_selector(-98, "special:magic"), "special:magic")
        self.assertEqual(W.workspace_selector(-1337, "music"), "name:music")
        self.assertEqual(W.workspace_selector(None, None), "1")

    def test_config_defaults_and_validation(self):
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            cfg = W.build_config({"watch": {"mode": "bogus", "notify_after": "2", "min_confidence": 3,
                                            "vision_interval_seconds": [500, 100],
                                            "distraction_title_patterns": ["(?i)ok", "([unclosed"]},
                                  "groups": {"Bad Id": {}, "yt": {"title_patterns": ["(", "x"], "enabled": False}}})
        self.assertEqual(cfg.mode, "observe")
        self.assertEqual(cfg.notify_after, 2)
        self.assertEqual(cfg.min_confidence, 0.8)
        self.assertEqual(cfg.vision_interval_seconds, [180, 420])
        self.assertEqual([p.pattern for p in cfg.regexes["distraction_title_patterns"]], ["(?i)ok"])
        self.assertEqual([g.id for g in cfg.groups], ["yt"])
        self.assertEqual([p.pattern for p in cfg.groups[0].title_patterns], ["x"])
        self.assertFalse(cfg.groups[0].enabled)
        self.assertIn("bad regex", err.getvalue())


# ============================================================================ tick branches

class IdleTests(Base):
    def test_no_active_window_is_idle(self):
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"]), ("idle", "idle"))
        self.assertEqual(self.observes()[-1]["verdict"], "idle")
        self.assertEqual(self.observes()[-1]["source"], "idle")

    def test_session_lock_is_idle(self):
        self.env.set_window(YT_WEBAPP, "YouTube")
        self.env.hypr["monitors"][0]["solitaryBlockedBy"] = ["LOCK"]
        self.env.save_hypr()
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["description"]), ("idle", "session locked"))

    def test_hyprlock_is_idle(self):
        self.env.set_window(YT_WEBAPP, "YouTube")
        with mock.patch.object(W, "_hyprlock_running", return_value=True):
            _, r = self.tick()
        self.assertEqual(r["verdict"], "idle")

    def test_idle_keeps_streak(self):
        self.env.write_state(streak=1)
        _, r = self.tick()
        self.assertEqual(r["streak"], 1)
        self.assertEqual(self.env.read_state()["streak"], 1)


class TitleTests(Base):
    def test_unlocked_group_title_is_allowed_and_resets_streak(self):
        self.env.daemon.unlock("youtube")
        self.env.write_state(streak=2)
        self.env.set_window("vivaldi-stable", YT_TITLE)
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["app"]), ("allowed", "unlock", "YouTube"))
        self.assertEqual(r["streak"], 0)

    def test_unlocked_group_webapp_class_is_allowed(self):
        self.env.daemon.unlock("youtube")
        self.env.set_window(YT_WEBAPP, "YouTube")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"]), ("allowed", "unlock"))

    def test_locked_group_title_pattern(self):
        self.env.set_window("vivaldi-stable", YT_TITLE)
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["app"], r["confidence"]),
                         ("procrastinating", "title", "YouTube", 1.0))
        self.assertEqual(r["streak"], 1)
        obs = self.observes()[-1]
        self.assertEqual(obs["verdict"], "procrastinating")
        self.assertEqual(obs["class"], "vivaldi-stable")
        self.assertEqual(obs["title"], YT_TITLE)
        self.assertEqual(obs["streak"], 1)
        self.assertEqual(self.env.grim_log(), [])  # title decided: no screenshot

    def test_locked_group_webapp_class(self):
        self.env.set_window(YT_WEBAPP, "Home")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["app"]), ("procrastinating", "YouTube"))

    def test_disabled_group_is_ignored(self):
        self.env.daemon.status["groups"][0]["enabled"] = False
        self.env.write_state(next_vision_at=time.time() + 999)
        self.env.set_window(YT_WEBAPP, "Home")
        _, r = self.tick()
        self.assertEqual(r["verdict"], "undecided")

    def test_distraction_pattern(self):
        self.env.set_window("vivaldi-stable", "Hacker News - Vivaldi")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["app"]), ("procrastinating", "title", "Hacker News"))

    def test_working_by_never_touch_class(self):
        self.env.write_state(streak=2)
        self.env.set_window("foot", "nvim watch.py")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["streak"]), ("working", "title", 0))

    def test_working_by_work_class_and_title(self):
        self.env.set_window("org.pwmt.zathura", "whatever")
        _, r = self.tick()
        self.assertEqual(r["verdict"], "working")
        self.env.set_window("vivaldi-stable", "[2401.01234] Attention - arXiv - Vivaldi")
        _, r = self.tick()
        self.assertEqual(r["verdict"], "working")

    def test_exempt_class_beats_group_pattern(self):
        self.env.set_window("obsidian", "Cats - YouTube")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"]), ("working", "title"))

    def test_title_patterns_only_apply_to_browsers_and_web_apps(self):
        # Native apps whose titles mention distractions: judged by class rules and vision, never by title.
        self.env.ollama.reply = {"app": "Files", "domain": "", "description": "a file manager",
                                 "verdict": "working", "confidence": 0.9}
        for cls, title in [("org.gnome.Nautilus", "YouTube - Files"), ("libreoffice-writer", "Netflix notes.odt"),
                           ("sioyek", "OnlyFans economics.pdf - Hacker News"), ("gnome-text-editor", "x - YouTube")]:
            self.env.write_state(next_vision_at=0)
            self.env.set_window(cls, title)
            _, r = self.tick()
            self.assertEqual((r["verdict"], r["source"]), ("working", "vision"), cls)
        # a work-title pattern doesn't make a native app "working" either: vision decides
        self.env.write_state(next_vision_at=time.time() + 999)
        self.env.set_window("some-native-app", "paper.pdf")
        _, r = self.tick()
        self.assertEqual(r["verdict"], "undecided")
        # but the same titles in a browser or a web app are matched
        self.env.set_window("chromium", "Cats - YouTube - Chromium")
        self.assertEqual(self.tick()[1]["verdict"], "procrastinating")
        self.env.set_window("chrome-news.ycombinator.com__-Default", "Hacker News")
        self.assertEqual(self.tick()[1]["app"], "Hacker News")

    def test_hyprctl_failure_is_error(self):
        self.env.hypr["fail"] = ["activewindow"]
        self.env.save_hypr()
        _, r = self.tick()
        self.assertEqual(r["verdict"], "error")


class VisionTests(Base):
    def setUp(self):
        super().setUp()
        self.win = self.env.set_window("vivaldi-stable", "Some page - Vivaldi", at=(12, 38), size=(1184, 1075))

    def assert_no_temp_files(self):
        self.assertEqual(self.env.runtime_files(), [])

    def test_not_due_is_undecided(self):
        self.env.write_state(next_vision_at=time.time() + 300, streak=1)
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"]), ("undecided", "title"))
        self.assertEqual(r["streak"], 1)
        self.assertEqual(self.env.grim_log(), [])
        self.assertEqual(self.env.ollama.requests, [])
        self.assertEqual(self.observes()[-1]["verdict"], "undecided")

    def test_due_procrastinating(self):
        self.env.ollama.reply = {"app": "Hacker News", "domain": "https://www.News.YCombinator.com/news",
                                 "description": "Tech news list", "verdict": "procrastinating", "confidence": 0.92}
        w, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["app"], r["domain"]),
                         ("procrastinating", "vision", "Hacker News", "news.ycombinator.com"))
        self.assertAlmostEqual(r["confidence"], 0.92)
        self.assertEqual(r["streak"], 1)
        # next screenshot scheduled inside the configured gap
        nv = self.env.read_state()["next_vision_at"]
        self.assertTrue(time.time() + 170 <= nv <= time.time() + 421, nv)
        # screenshot: only the window rectangle, into a 0600 file under XDG_RUNTIME_DIR, then deleted
        g = self.env.grim_log()
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["argv"][:4], ["-t", "ppm", "-g", "12,38 1184x1075"])
        self.assertTrue(g[0]["path"].startswith(str(self.env.runtime) + "/"))
        self.assertEqual(g[0]["mode"], "0o600")
        self.assert_no_temp_files()
        # request shape (§12 / prototype)
        req = self.env.ollama.requests[-1]
        self.assertEqual(req["path"], "/api/chat")
        body = req["body"]
        self.assertEqual(body["model"], "qwen3.5:4b")
        self.assertIs(body["think"], False)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["options"], {"temperature": 0, "num_ctx": 4096})
        self.assertEqual(body["format"], W.VISION_SCHEMA)
        self.assertIn("domain", body["format"]["required"])
        user = body["messages"][-1]
        self.assertIn("Window class: vivaldi-stable", user["content"])
        self.assertIn("Window title: Some page - Vivaldi", user["content"])
        img = base64.b64decode(user["images"][0])
        self.assertEqual(img[:2], b"\xff\xd8")  # JPEG
        if W._have_pil():
            from PIL import Image
            with Image.open(io.BytesIO(img)) as im:
                self.assertEqual(im.size, (1024, 640))  # 1600x1000 fake capture scaled to 1024 wide

    def test_due_working_resets_streak(self):
        self.env.write_state(streak=2)
        self.env.ollama.reply = {"app": "GitHub", "domain": "github.com", "description": "code",
                                 "verdict": "working", "confidence": 0.95}
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["streak"]), ("working", 0))

    def test_due_unclear_and_low_confidence_keep_streak(self):
        self.env.write_state(streak=1)
        self.env.ollama.reply = {"app": "?", "domain": "", "description": "desktop", "verdict": "unclear",
                                 "confidence": 0.3}
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["streak"], r["domain"]), ("unclear", 1, ""))
        self.env.write_state(streak=1)
        self.env.ollama.reply = {"app": "X", "domain": "x.com", "description": "feed",
                                 "verdict": "procrastinating", "confidence": 0.5}
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["streak"]), ("procrastinating", 1))

    def test_bad_model_values_are_sanitised(self):
        self.env.ollama.reply = {"app": "A" * 1000, "domain": "Hacker News", "description": None,
                                 "verdict": "maybe", "confidence": 7}
        _, r = self.tick()
        self.assertEqual(r["verdict"], "unclear")
        self.assertEqual(r["confidence"], 1.0)
        self.assertEqual(r["domain"], "")
        self.assertLessEqual(len(r["app"]), 300)

    def test_browser_name_as_app_is_replaced_by_domain(self):
        for app in ("Vivaldi", "vivaldi-stable", "Chromium", ""):
            self.env.write_state(next_vision_at=0)
            self.env.ollama.reply = {"app": app, "domain": "www.reddit.com", "description": "feed",
                                     "verdict": "procrastinating", "confidence": 0.9}
            _, r = self.tick()
            self.assertEqual(r["app"], "reddit.com", app)
        self.env.write_state(next_vision_at=0)
        self.env.ollama.reply = {"app": "Vivaldi", "domain": "", "description": "settings",
                                 "verdict": "unclear", "confidence": 0.9}
        _, r = self.tick()
        self.assertEqual(r["app"], "Vivaldi")

    def _assert_error(self, r):
        self.assertEqual((r["verdict"], r["source"]), ("error", "vision"))
        self.assertEqual(self.observes()[-1]["verdict"], "error")
        self.assert_no_temp_files()
        self.assertGreater(self.env.read_state()["next_vision_at"], time.time() + 100)

    def test_http_error(self):
        self.env.ollama.behaviour = "http500"
        _, r = self.tick()
        self._assert_error(r)

    def test_unparseable_reply(self):
        self.env.ollama.behaviour = "badjson"
        _, r = self.tick()
        self._assert_error(r)

    def test_timeout(self):
        self.env.write_config(vision_timeout_seconds=1)
        self.env.ollama.behaviour, self.env.ollama.delay = "slow", 3
        t0 = time.time()
        _, r = self.tick()
        self.assertLess(time.time() - t0, 3)
        self._assert_error(r)

    def test_redirect_is_refused(self):
        self.env.ollama.behaviour = "redirect"
        _, r = self.tick()
        self._assert_error(r)

    def test_grim_failure_deletes_partial_file(self):
        with mock.patch.dict(os.environ, {"FAKE_GRIM_FAIL": "1"}):
            _, r = self.tick()
        self._assert_error(r)
        self.assertEqual(len(self.env.grim_log()), 1)
        self.assertEqual(self.env.ollama.requests, [])

    def test_proxy_env_is_ignored(self):
        with mock.patch.dict(os.environ, {"http_proxy": "http://192.0.2.1:9", "HTTP_PROXY": "http://192.0.2.1:9",
                                          "no_proxy": "", "NO_PROXY": ""}):
            _, r = self.tick()
        self.assertEqual(r["verdict"], "procrastinating")
        self.assertEqual(len(self.env.ollama.requests), 1)

    @unittest.skipUnless(shutil.which("magick"), "ImageMagick not installed")
    def test_magick_fallback(self):
        with mock.patch.object(W, "_have_pil", return_value=False):
            _, r = self.tick()
        self.assertEqual(r["verdict"], "procrastinating")
        img = base64.b64decode(self.env.ollama.requests[-1]["body"]["messages"][-1]["images"][0])
        self.assertEqual(img[:2], b"\xff\xd8")
        self.assert_no_temp_files()

    def test_no_runtime_dir_refuses_to_capture(self):
        w = self.watcher()
        w.paths["runtime"] = ""
        r = w.tick()
        self.assertEqual(r["verdict"], "error")
        self.assertEqual(self.env.grim_log(), [])

    def test_low_battery_skips_screenshot(self):
        self.env.set_battery(10, "Discharging")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"]), ("undecided", "vision"))
        self.assertIn("battery", r["description"])
        self.assertEqual(self.env.grim_log(), [])
        self.assertEqual(self.env.ollama.requests, [])
        # charging at the same level → screenshots allowed
        self.env.set_battery(10, "Charging")
        _, r = self.tick()
        self.assertEqual(r["verdict"], "procrastinating")
        # discharging above the threshold → allowed
        self.env.set_battery(50, "Discharging")
        self.env.write_state(next_vision_at=0)
        _, r = self.tick()
        self.assertEqual(r["source"], "vision")

    def test_once_ignores_the_vision_timer(self):
        self.env.write_state(next_vision_at=time.time() + 300)
        _, r = self.tick(once=True)
        self.assertEqual(r["source"], "vision")


# ============================================================================ streak and escalation

class StreakTests(Base):
    def test_streak_rules(self):
        w = self.watcher()
        seq = []

        def step(cls, title, **state):
            self.env.set_window(cls, title)
            if state:
                w.state.__dict__.update(state)
            r = w.tick()
            seq.append((r["verdict"], r["streak"]))

        step("vivaldi-stable", YT_TITLE)                                     # procrastinating → 1
        step(YT_WEBAPP, "x")                                                 # → 2
        self.env.ollama.reply = dict(self.env.ollama.reply, verdict="unclear")
        step("vivaldi-stable", "Undecided page", next_vision_at=0)          # unclear: unchanged
        step("vivaldi-stable", "Undecided page")                             # undecided (not due): unchanged
        self.env.ollama.behaviour = "http500"
        step("vivaldi-stable", "Undecided page", next_vision_at=0)          # error: unchanged
        self.env.hypr["activewindow"] = {}
        self.env.save_hypr()
        r = w.tick()
        seq.append((r["verdict"], r["streak"]))                              # idle: unchanged
        step("foot", "shell")                                                # working → 0
        step("vivaldi-stable", YT_TITLE)                                     # → 1
        self.env.daemon.unlock("reddit")
        step("vivaldi-stable", "r/python - Reddit - Vivaldi")               # allowed → 0
        self.assertEqual(seq, [("procrastinating", 1), ("procrastinating", 2), ("unclear", 2), ("undecided", 2),
                               ("error", 2), ("idle", 2), ("working", 0), ("procrastinating", 1), ("allowed", 0)])
        st = self.env.state_file()
        self.assertEqual(st.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.env.read_state()["streak"], 0)
        self.assertEqual([o["streak"] for o in self.observes()], [s for _, s in seq])


class NotifyTests(Base):
    def run_streak(self, mode, n=4):
        self.env.daemon.set_mode(mode)
        self.env.set_window("vivaldi-stable", YT_TITLE)
        w = self.watcher()
        return [w.tick() for _ in range(n)]

    def test_observe_mode_never_notifies(self):
        self.run_streak("observe")
        self.assertEqual(self.env.notify_log(), [])
        self.assertEqual(self.env.daemon.ops("penalize"), [])

    def test_nudge_notifies_every_notify_after_and_never_enforces(self):
        rs = self.run_streak("nudge", 5)
        self.assertEqual([r["streak"] for r in rs], [1, 2, 3, 4, 5])
        self.assertEqual(self.env.notify_log(), [["-u", "normal", "Focus", "Looks like YouTube. Back to work?"]] * 2)
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.assertEqual(self.env.dispatches(), [])

    def test_low_confidence_does_not_renotify(self):
        self.env.daemon.set_mode("nudge")
        self.env.write_state(streak=2)
        self.env.set_window("vivaldi-stable", "page")
        self.env.ollama.reply = dict(self.env.ollama.reply, confidence=0.4)
        _, r = self.tick()
        self.assertEqual(r["streak"], 2)
        self.assertEqual(self.env.notify_log(), [])

    def test_enforce_mode_notifies_at_notify_after(self):
        self.run_streak("enforce", 2)
        self.assertEqual(self.env.notify_log(), [["-u", "normal", "Focus", "Looks like YouTube. Back to work?"]])

    def test_mode_from_config_when_status_lacks_it(self):
        del self.env.daemon.status["watch"]
        self.env.write_config(mode="nudge")
        self.env.write_state(streak=1)
        self.env.set_window("vivaldi-stable", YT_TITLE)
        _, r = self.tick()
        self.assertEqual(r["mode"], "nudge")
        self.assertEqual(len(self.env.notify_log()), 1)


class EnforceTests(Base):
    mode = "enforce"

    def enforce_tick(self, cls, title, pid=4242, **kw):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(cls, title, pid=pid, **kw)
        return self.tick()

    def test_plain_app_is_hidden(self):
        w, r = self.enforce_tick(YT_WEBAPP, "Home", address="0x55aa0000beef", ws=(3, "3"))
        pens = self.env.daemon.ops("penalize")
        self.assertEqual(len(pens), 1)
        self.assertEqual({k: pens[0][k] for k in ("app", "title", "source", "domain")},
                         {"app": "YouTube", "title": "Home", "source": "title", "domain": None})
        self.assertTrue(pens[0]["reason"])
        self.assertEqual(self.env.dispatches(), [
            'hl.dsp.window.move({ workspace = "special:focusjail", follow = false, window = "address:0x55aa0000beef" })'])
        self.assertEqual(self.env.load_hypr()["clients"][0]["workspace"]["name"], "special:focusjail")
        st = self.env.read_state()
        self.assertEqual(st["streak"], 0)
        self.assertEqual(r["streak"], 0)
        [entry] = st["hidden"]
        pid_ = self.env.daemon.status["penalties"][0]["id"]
        self.assertEqual({k: entry[k] for k in ("penalty_id", "address", "class", "workspace", "pid", "frozen")},
                         {"penalty_id": pid_, "address": "0x55aa0000beef", "class": YT_WEBAPP, "workspace": "3",
                          "pid": 4242, "frozen": False})
        self.assertEqual(self.env.notify_log()[-1],
                         ["-u", "critical", "Focus", "YouTube is blocked for 30 min. Appeal: focus appeal"])

    def test_never_touch_class_is_never_flagged(self):
        _, r = self.enforce_tick("obsidian", "Cats - YouTube")
        self.assertEqual(r["verdict"], "working")
        self.assertEqual(self.env.daemon.ops("penalize"), [])

    def test_never_touch_rule_gets_penalty_but_no_window_action(self):
        # unreachable through classification now, but the enforcement rule must still hold
        win = self.env.set_window("obsidian", "Cats - YouTube")
        w = self.watcher()
        obs = {"verdict": "procrastinating", "source": "vision", "app": "YouTube", "class": "obsidian",
               "title": "Cats - YouTube", "description": "", "domain": "", "confidence": 0.9}
        w.enforce(obs, win, 3, self.env.daemon.respond({"op": "status"}))
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(w.state.hidden, [])

    def test_browser_gets_domain_penalty_but_is_not_hidden(self):
        self.env.ollama.reply = {"app": "Hacker News", "domain": "news.ycombinator.com", "description": "HN front page",
                                 "verdict": "procrastinating", "confidence": 0.9}
        self.enforce_tick("vivaldi-stable", "Some page - Vivaldi")
        [pen] = self.env.daemon.ops("penalize")
        self.assertEqual((pen["source"], pen["domain"], pen["app"]), ("vision", "news.ycombinator.com", "Hacker News"))
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_title_hit_in_browser_gets_domain_from_one_vision_call(self):
        self.env.write_state(streak=2, notified=True, next_vision_at=time.time() + 999)
        self.env.set_window("vivaldi-stable", "Hacker News - Vivaldi")
        self.tick()
        [pen] = self.env.daemon.ops("penalize")
        self.assertEqual((pen["source"], pen["domain"], pen["app"]), ("title", "news.ycombinator.com", "Hacker News"))
        self.assertEqual(len(self.env.ollama.requests), 1)  # despite next_vision_at

    def test_title_hit_in_browser_without_identifiable_domain(self):
        self.env.ollama.behaviour = "http500"
        self.enforce_tick("vivaldi-stable", "Hacker News - Vivaldi")
        [pen] = self.env.daemon.ops("penalize")
        self.assertIsNone(pen["domain"])

    def test_web_app_title_hit_needs_no_vision_call(self):
        self.enforce_tick(YT_WEBAPP, "Home")
        self.assertEqual(self.env.ollama.requests, [])

    def test_freeze_class_is_hidden_and_stopped_then_restored(self):
        p = self.env.start_sleep()
        self.enforce_tick("Minecraft* 1.21.4", "Minecraft", pid=p.pid, address="0x55aa0000c0de", ws=(2, "2"))
        self.assertTrue(self.env.wait_state(p.pid, "T"), "sleep should be stopped")
        [entry] = self.env.read_state()["hidden"]
        self.assertTrue(entry["frozen"])
        self.assertEqual(entry["pid_start"], self.env.proc_start(p.pid))
        # penalty still active → nothing restored
        self.tick()
        self.assertEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(len(self.env.dispatches()), 1)
        # penalty ends → SIGCONT and the window goes back to workspace 2
        self.env.daemon.end_penalty(entry["penalty_id"])
        self.env.set_window("foot", "shell", address="0x55aa00000001",
                            extra_clients=[dict(self.env.load_hypr()["clients"][0])])
        _, r = self.tick()
        self.assertTrue(self.env.wait_state(p.pid, "SR"), "sleep should run again")
        self.assertEqual(self.env.dispatches()[-1],
                         'hl.dsp.window.move({ workspace = "2", follow = false, window = "address:0x55aa0000c0de" })')
        self.assertEqual(self.env.read_state()["hidden"], [])
        self.assertTrue(any(a["action"] == "restore" and a.get("continued") and a.get("moved") for a in r["actions"]))

    def test_freeze_never_stops_a_browser_process(self):
        tmpbin = self.env.tmp / "bin"
        tmpbin.mkdir()
        exe = tmpbin / "chromium"
        shutil.copy("/bin/sleep", exe)
        exe.chmod(0o755)
        p = self.env.start_sleep(str(exe))
        self.assertEqual(Path(f"/proc/{p.pid}/comm").read_text().strip(), "chromium")
        self.enforce_tick("minecraft-launcher", "Minecraft Launcher", pid=p.pid)
        time.sleep(0.1)
        self.assertNotEqual(self.env.proc_state(p.pid), "T")
        [entry] = self.env.read_state()["hidden"]
        self.assertFalse(entry["frozen"])
        self.assertEqual(len(self.env.dispatches()), 1)  # still hidden

    def test_freeze_skips_other_users_and_self(self):
        w = self.watcher()
        self.assertFalse(w._can_freeze(1, "minecraft"))
        self.assertFalse(w._can_freeze(os.getpid(), "minecraft"))
        self.assertFalse(w._can_freeze(None, "minecraft"))
        self.assertFalse(w._can_freeze(999999999, "minecraft"))

    def test_refused_penalize_only_notifies(self):
        self.env.daemon.penalize_error = "too_many_penalties"
        _, r = self.enforce_tick(YT_WEBAPP, "Home")
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])
        self.assertEqual(self.env.notify_log()[-1],
                         ["-u", "normal", "Focus", "Looks like YouTube. Back to work? (no penalty: too_many_penalties)"])
        self.assertEqual(r["streak"], 0)

    def test_window_closed_before_hide_is_not_touched(self):
        self.env.write_state(streak=2)
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000beef")
        w = self.watcher()
        real_client = w.hypr.client

        def gone(addr):  # the window closes (and the address is reused) right after classification
            c = real_client(addr)
            return dict(c, **{"class": "foot"}) if c else None
        with mock.patch.object(w.hypr, "client", side_effect=gone):
            w.tick()
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_moved_window_is_recorded_even_if_verification_fails(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000beef")
        w = self.watcher()
        real_client = w.hypr.client
        with mock.patch.object(w.hypr, "client", side_effect=[real_client("0x55aa0000beef"), W.HyprError("boom")]):
            w.tick()
        self.assertEqual(len(self.env.dispatches()), 1)
        [entry] = self.env.read_state()["hidden"]
        self.assertEqual(entry["address"], "0x55aa0000beef")

    def test_failed_dispatch_records_nothing(self):
        self.env.hypr["dispatch_rc"] = 7
        self.env.hypr["dispatch_out"] = "error: something"
        self.env.save_hypr()
        self.enforce_tick(YT_WEBAPP, "Home")
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_nudge_mode_never_enforces(self):
        self.env.daemon.set_mode("nudge")
        self.enforce_tick(YT_WEBAPP, "Home")
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["streak"], 3)


class RestoreTests(Base):
    mode = "enforce"

    def jailed(self, address="0x55aa0000aaaa", cls="steam_app_42", ws=(4, "4"), pid=None, frozen=False,
               penalty="p00000001", until=None):
        client = {"address": address, "class": cls, "title": "Game", "pid": pid or 4243, "at": [0, 0],
                  "size": [100, 100], "workspace": {"id": -98, "name": "special:focusjail"}}
        entry = {"penalty_id": penalty, "address": address, "class": cls, "title": "Game", "workspace": ws[1],
                 "workspace_id": ws[0], "pid": pid or 4243, "pid_start": self.env.proc_start(pid) if pid else None,
                 "frozen": frozen, "since": time.time()}
        if until is not None:
            entry["until"] = until
        return client, entry

    def setup_jail(self, frozen=True, penalty_active=True, until=None):
        p = self.env.start_sleep()
        if frozen:
            os.kill(p.pid, 19)  # SIGSTOP our own sleep
            self.assertTrue(self.env.wait_state(p.pid, "T"))
        client, entry = self.jailed(pid=p.pid, frozen=frozen, until=until)
        self.env.set_window("foot", "shell", address="0x55aa00000001", extra_clients=[client])
        self.env.write_state(hidden=[entry])
        if penalty_active:
            self.env.daemon.add_penalty("p00000001")
        return p, entry

    def test_penalty_still_active_keeps_window_jailed(self):
        p, _ = self.setup_jail()
        self.tick()
        self.assertEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(len(self.env.read_state()["hidden"]), 1)

    def test_penalty_gone_restores(self):
        p, _ = self.setup_jail(penalty_active=False)
        self.tick()
        self.assertTrue(self.env.wait_state(p.pid, "SR"))
        self.assertEqual(self.env.dispatches(), [
            'hl.dsp.window.move({ workspace = "4", follow = false, window = "address:0x55aa0000aaaa" })'])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_mode_off_restores_everything_and_does_not_observe(self):
        p, _ = self.setup_jail(penalty_active=True)
        self.env.daemon.set_mode("off")
        _, r = self.tick()
        self.assertIsNone(r["verdict"])
        self.assertTrue(self.env.wait_state(p.pid, "SR"))
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(self.env.read_state()["hidden"], [])
        self.assertEqual(self.observes(), [])

    def test_restore_all_on_shutdown(self):
        p, _ = self.setup_jail(penalty_active=True)
        w = self.watcher()
        w.restore_all("shutdown")
        self.assertTrue(self.env.wait_state(p.pid, "SR"))
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_reused_address_is_not_moved(self):
        p, entry = self.setup_jail(penalty_active=False)
        hy = self.env.load_hypr()
        hy["clients"][1]["class"] = "some-other-app"  # same address, different window now
        self.env.hypr = hy
        self.env.save_hypr()
        self.tick()
        self.assertTrue(self.env.wait_state(p.pid, "SR"))  # the process we stopped is still resumed
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_window_already_out_of_jail_is_left_alone(self):
        p, _ = self.setup_jail(frozen=False, penalty_active=False)
        hy = self.env.load_hypr()
        hy["clients"][1]["workspace"] = {"id": 5, "name": "5"}
        self.env.hypr = hy
        self.env.save_hypr()
        self.tick()
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_reused_pid_is_not_continued(self):
        p, entry = self.setup_jail(penalty_active=False)
        st = self.env.read_state()
        st["hidden"][0]["pid_start"] = entry["pid_start"] + 1  # pretend the pid now belongs to another process
        self.env.state_file().write_text(json.dumps(st))
        self.tick()
        time.sleep(0.1)
        self.assertEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])  # its process is gone: confirmed gone

    def test_daemon_unreachable_does_not_restore_or_enforce(self):
        p, _ = self.setup_jail(penalty_active=False)
        self.env.daemon.stop()
        self.env.write_state(streak=2, notified=True, hidden=self.env.read_state()["hidden"])
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000bbbb",
                            extra_clients=[self.env.load_hypr()["clients"][1]])
        _, r = self.tick()
        self.assertEqual(r["daemon"], "unreachable")
        self.assertEqual((r["verdict"], r["streak"]), ("procrastinating", 3))
        self.assertTrue(any(a["action"] == "skip_enforce" for a in r["actions"]))
        self.assertEqual(self.env.dispatches(), [])
        time.sleep(0.1)
        self.assertEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(len(self.env.read_state()["hidden"]), 1)
        self.assertEqual(self.env.read_state()["streak"], 3)

    # ---- amendments: drop only when restored or confirmed gone; `until` expiry
    def test_clients_failure_keeps_entry_and_retries(self):
        p, _ = self.setup_jail(penalty_active=False)
        self.env.hypr["fail"] = ["clients"]
        self.env.save_hypr()
        self.tick()
        self.assertTrue(self.env.wait_state(p.pid, "SR"))  # processes are resumed regardless
        self.assertEqual(self.env.dispatches(), [])
        [entry] = self.env.read_state()["hidden"]
        self.assertFalse(entry["frozen"])
        hy = self.env.load_hypr()
        hy["fail"] = []
        self.env.hypr = hy
        self.env.save_hypr()
        self.tick()
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_failed_move_back_keeps_entry(self):
        self.setup_jail(frozen=False, penalty_active=False)
        self.env.hypr = self.env.load_hypr()
        self.env.hypr.update(dispatch_rc=7, dispatch_out="error: nope")
        self.env.save_hypr()
        self.tick()
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(len(self.env.read_state()["hidden"]), 1)

    def test_window_gone_is_dropped(self):
        self.setup_jail(frozen=False, penalty_active=False)
        self.env.set_window("foot", "shell", address="0x55aa00000001")  # jailed window no longer listed
        self.tick()
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_expired_until_restores_while_daemon_unreachable(self):
        p, _ = self.setup_jail(penalty_active=True, until=time.time() - 1)
        self.env.daemon.stop()
        _, r = self.tick()
        self.assertEqual(r["daemon"], "unreachable")
        self.assertTrue(self.env.wait_state(p.pid, "SR"))
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_future_until_is_kept_while_daemon_unreachable(self):
        p, _ = self.setup_jail(penalty_active=True, until=time.time() + 600)
        self.env.daemon.stop()
        self.tick()
        time.sleep(0.1)
        self.assertEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(len(self.env.read_state()["hidden"]), 1)

    def test_passed_until_restores_even_if_daemon_still_lists_it(self):
        p, _ = self.setup_jail(penalty_active=True, until=time.time() - 1)
        self.tick()
        self.assertTrue(self.env.wait_state(p.pid, "SR"))
        self.assertEqual(self.env.read_state()["hidden"], [])


class DryRunTests(Base):
    mode = "enforce"

    def test_dry_run_takes_no_actions(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home")
        before = self.env.state_file().read_text()
        _, r = self.tick(dry_run=True)
        kinds = [a["action"] for a in r["actions"]]
        self.assertIn("penalize", kinds)
        self.assertIn("hide", kinds)
        self.assertIn("notify", kinds)
        self.assertTrue(all(a.get("dry_run") for a in r["actions"] if a["action"] in ("penalize", "hide", "notify")))
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.notify_log(), [])
        self.assertEqual(self.env.state_file().read_text(), before)
        self.assertEqual(len(self.observes()), 1)

    def test_dry_run_restore_is_reported_only(self):
        p = self.env.start_sleep()
        os.kill(p.pid, 19)
        self.assertTrue(self.env.wait_state(p.pid, "T"))
        entry = {"penalty_id": "pgone", "address": "0x55aa0000aaaa", "class": "steam_app_1", "workspace": "1",
                 "workspace_id": 1, "pid": p.pid, "pid_start": self.env.proc_start(p.pid), "frozen": True}
        self.env.write_state(hidden=[entry])
        _, r = self.tick(dry_run=True)
        self.assertTrue(any(a["action"] == "restore" for a in r["actions"]))
        time.sleep(0.1)
        self.assertEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(len(self.env.read_state()["hidden"]), 1)

    def test_no_report_skips_observe(self):
        self.env.set_window("foot", "shell")
        self.tick(no_report=True)
        self.assertEqual(self.observes(), [])
        self.assertEqual(len(self.env.daemon.ops("status")), 1)


class AmendmentTests(Base):
    """Review amendments (§12 'Amendments after review')."""
    mode = "enforce"

    def test_freeze_is_off_by_default(self):
        self.assertEqual(W.build_config({}).regexes["freeze_classes"], [])
        self.assertEqual(W.DEFAULT_WATCH["freeze_classes"], [])

    def test_entry_written_before_dispatch_with_until(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000beef")
        w = self.watcher()
        seen = []
        real = w.hypr.dispatch

        def spy(expr):
            if "focusjail" in expr:
                seen.append([h["address"] for h in json.loads(self.env.state_file().read_text())["hidden"]])
            return real(expr)
        with mock.patch.object(w.hypr, "dispatch", side_effect=spy):
            w.tick()
        self.assertEqual(seen, [["0x55aa0000beef"]])
        [entry] = self.env.read_state()["hidden"]
        pen = self.env.daemon.status["penalties"][0]
        self.assertEqual(entry["until"], pen["until"])

    def test_cadence_and_warning_before_enforcement(self):
        # switched to enforce mid-streak without an earlier warning: warn first, enforce afterwards
        self.env.write_state(streak=4, notified=False)
        self.env.set_window(YT_WEBAPP, "Home")
        w = self.watcher()
        r5 = w.tick()
        self.assertEqual((r5["streak"], self.env.notify_log(), self.env.daemon.ops("penalize")), (5, [], []))
        r6 = w.tick()
        self.assertEqual(r6["streak"], 6)
        self.assertEqual(len(self.env.notify_log()), 1)
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        r7 = w.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        self.assertEqual(r7["streak"], 0)
        self.assertFalse(self.env.read_state()["notified"])

    def test_working_resets_the_warning(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window("foot", "shell")
        self.tick()
        st = self.env.read_state()
        self.assertEqual((st["streak"], st["notified"]), (0, False))

    def test_duplicate_penalty_by_app_notifies_once_and_hides_under_it(self):
        pen = self.env.daemon.add_penalty("pdup00001", app="YouTube", domain=None, until=time.time() + 900)
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000d001")
        _, r = self.tick()
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        [entry] = self.env.read_state()["hidden"]  # not hidden yet → jailed under the existing penalty
        self.assertEqual((entry["penalty_id"], entry["address"], entry["until"]),
                         ("pdup00001", "0x55aa0000d001", pen["until"]))
        self.assertEqual(len(self.env.dispatches()), 1)
        self.assertEqual(len(self.env.notify_log()), 1)
        self.assertIn("already blocked", self.env.notify_log()[0][3])
        self.assertEqual(r["streak"], 0)
        st = self.env.read_state()
        st.update(streak=2, notified=True)
        self.env.state_file().write_text(json.dumps(st))
        self.tick()
        self.assertEqual(len(self.env.notify_log()), 1)  # once per penalty
        self.assertEqual(len(self.env.dispatches()), 1)  # already hidden
        self.assertEqual(self.env.daemon.ops("penalize"), [])

    def test_domain_dropped_by_focusd_is_still_a_duplicate(self):
        # reviewer's repro: focusd nulls the domain → previously a fresh penalty every enforce_after ticks
        self.env.daemon.drop_domain = True
        self.env.set_window("vivaldi-stable", "Some page - Vivaldi")
        w = self.watcher()
        for _ in range(9):
            w.state.next_vision_at = 0
            w.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        self.assertEqual(len([n for n in self.env.notify_log() if n[1] == "critical"]), 1)

    def test_duplicate_by_window_class_and_title_stem(self):
        self.env.daemon.drop_domain = True
        self.env.write_state(streak=2, notified=True)
        self.env.set_window("vivaldi-stable", "Ask HN: what are you reading? | Orange Site - Vivaldi")
        w = self.watcher()
        w.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        # the model names it differently and sees no domain this time: same class + title stem → duplicate
        self.env.ollama.reply = {"app": "HN front page", "domain": "", "description": "links",
                                 "verdict": "procrastinating", "confidence": 0.9}
        self.env.set_window("vivaldi-stable", "Show HN: a tiny Lisp | Orange Site - Vivaldi")
        w.state.streak, w.state.notified, w.state.next_vision_at = 2, True, 0
        w.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        # a different site in the same browser is not a duplicate
        self.env.ollama.reply = dict(self.env.ollama.reply, app="Kittens")
        self.env.set_window("vivaldi-stable", "Cute cats | Kittens Daily - Vivaldi")
        w.state.streak, w.state.notified, w.state.next_vision_at = 2, True, 0
        w.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 2)

    def test_dedupe_runs_before_the_domain_screenshot(self):
        self.env.daemon.add_penalty("pdup00003", app="Hacker News", domain=None)
        self.env.write_state(streak=2, notified=True, next_vision_at=time.time() + 999)
        self.env.set_window("vivaldi-stable", "Hacker News - Vivaldi")
        self.tick()
        self.assertEqual(self.env.ollama.requests, [])
        self.assertEqual(self.env.grim_log(), [])
        self.assertEqual(self.env.daemon.ops("penalize"), [])

    def test_reopened_web_app_is_hidden_under_the_existing_penalty(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000e001")
        w = self.watcher()
        w.tick()
        [pen] = self.env.daemon.status["penalties"]
        first = self.env.load_hypr()["clients"][0]
        self.assertEqual(first["workspace"]["name"], "special:focusjail")
        self.env.set_window(YT_WEBAPP, "Home", address="0x55aa0000e002", extra_clients=[first])
        w.state.streak, w.state.notified = 2, True
        w.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)
        self.assertEqual([(h["address"], h["penalty_id"], h["until"]) for h in self.env.read_state()["hidden"]],
                         [("0x55aa0000e001", pen["id"], pen["until"]), ("0x55aa0000e002", pen["id"], pen["until"])])

    def test_window_already_in_the_jail_is_never_hidden(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home", ws=(-98, "special:focusjail"))
        self.tick()
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_restore_never_targets_the_jail(self):
        self.assertEqual(W.workspace_selector(-98, "special:focusjail"), "1")
        p = self.env.start_sleep()
        client = {"address": "0x55aa0000f001", "class": "steam_app_9", "title": "G", "pid": p.pid, "at": [0, 0],
                  "size": [5, 5], "workspace": {"id": -98, "name": "special:focusjail"}}
        entry = {"penalty_id": "pgone", "address": "0x55aa0000f001", "class": "steam_app_9",
                 "workspace": "special:focusjail", "workspace_id": -98, "pid": p.pid,
                 "pid_start": self.env.proc_start(p.pid), "frozen": False}
        self.env.set_window("foot", "shell", address="0x55aa00000001", extra_clients=[client])
        self.env.hypr["activeworkspace"] = {"id": 2, "name": "2"}
        self.env.save_hypr()
        self.env.write_state(hidden=[entry])
        self.tick()
        self.assertEqual(self.env.dispatches(), [
            'hl.dsp.window.move({ workspace = "2", follow = false, window = "address:0x55aa0000f001" })'])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_title_stem(self):
        self.assertEqual(W.title_stem("(3) Some video - YouTube - Vivaldi"), "youtube")
        self.assertEqual(W.title_stem("Ask HN | Orange Site - Vivaldi"), "orange site")
        self.assertEqual(W.title_stem("Vivaldi"), "")
        self.assertEqual(W.title_stem("Hacker News"), "hacker news")

    def test_duplicate_penalty_by_domain(self):
        self.env.daemon.add_penalty("pdup00002", app="HN", domain="news.ycombinator.com")
        self.env.write_state(streak=2, notified=True)
        self.env.set_window("vivaldi-stable", "Some page - Vivaldi")
        self.tick()
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.env.ollama.reply = dict(self.env.ollama.reply, domain="lobste.rs", app="Lobsters")
        self.env.write_state(streak=2, notified=True)
        self.tick()
        self.assertEqual(len(self.env.daemon.ops("penalize")), 1)

    def test_once_never_enforces_or_notifies(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home")
        _, r = self.tick(once=True)
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.notify_log(), [])
        self.assertTrue(any(a["action"] == "skip_enforce" for a in r["actions"]))

    def test_once_dry_run_shows_notification_only(self):
        self.env.write_state(streak=1)
        self.env.set_window(YT_WEBAPP, "Home")
        _, r = self.tick(once=True, dry_run=True)
        self.assertEqual([a["action"] for a in r["actions"]], ["notify"])
        self.assertEqual(self.env.notify_log(), [])

    def test_cached_status_for_ten_minutes_then_unknown(self):
        self.env.daemon.unlock("youtube")
        self.env.daemon.set_mode("nudge")
        self.env.set_window("vivaldi-stable", YT_TITLE)
        self.env.write_state(next_vision_at=time.time() + 999)
        w = self.watcher()
        self.assertEqual(w.tick()["verdict"], "allowed")
        self.env.daemon.stop()
        r = w.tick()
        self.assertEqual((r["daemon"], r["verdict"], r["mode"]), ("unreachable", "allowed", "nudge"))
        w._cache = (w._cache[0] - 601, w._cache[1])
        r = w.tick()
        self.assertEqual((r["verdict"], r["mode"]), ("undecided", "enforce"))  # config mode, groups unknown

    def test_no_status_ever_means_groups_unknown(self):
        self.env.daemon.stop()
        self.env.write_state(next_vision_at=time.time() + 999)
        self.env.set_window(YT_WEBAPP, "Home")
        _, r = self.tick()
        self.assertEqual(r["verdict"], "undecided")
        self.env.set_window("vivaldi-stable", "Hacker News - Vivaldi")  # distraction patterns still apply
        _, r = self.tick()
        self.assertEqual(r["verdict"], "procrastinating")

    def test_signal_handler_only_sets_a_flag(self):
        w = self.watcher()
        with mock.patch.object(W.threading.Event, "set", side_effect=AssertionError("no Event.set in handler")):
            w._on_signal(15, None)  # must not raise
        self.assertTrue(w._stopping)
        t0 = time.monotonic()
        w.sleep(30)
        self.assertLess(time.monotonic() - t0, 1)

    def test_notification_markup_is_escaped(self):
        self.env.daemon.set_mode("nudge")
        self.env.write_state(streak=1)
        self.env.ollama.reply = {"app": "<b>Tom & Jerry</b>", "domain": "", "description": "cartoon",
                                 "verdict": "procrastinating", "confidence": 0.9}
        self.env.set_window("some-player", "cartoon")
        self.tick()
        self.assertEqual(self.env.notify_log()[-1][3], "Looks like &lt;b&gt;Tom &amp; Jerry&lt;/b&gt;. Back to work?")

    def test_stale_temp_images_are_deleted(self):
        rt = self.env.runtime
        old = time.time() - 3600
        for name in ("focus-watch-abc.ppm", "focus-watch-def.jpg", "other.txt"):
            (rt / name).write_bytes(b"x")
            os.utime(rt / name, (old, old))
        (rt / "focus-watch-fresh.ppm").write_bytes(b"x")  # may belong to a concurrent check
        self.env.set_window("foot", "shell")
        self.tick()
        self.assertEqual(self.env.runtime_files(), ["focus-watch-fresh.ppm", "other.txt"])


class LearnedTests(Base):
    """Adaptive blocking (§13): learned sites arrive as status groups with `learned: true`."""
    LOBSTERS = "chrome-lobste.rs__-Default"

    def undecided_later(self):
        self.env.write_state(next_vision_at=time.time() + 999, learned=[])

    def test_locked_learned_group_matches_title_in_browser(self):
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.set_window("vivaldi-stable", "Some story - Lobsters - Vivaldi")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["app"]), ("procrastinating", "title", "Lobsters"))

    def test_locked_learned_group_matches_web_app_class(self):
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.set_window(self.LOBSTERS, "Front page")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["app"]), ("procrastinating", "Lobsters"))

    def test_unlocked_learned_group_is_allowed(self):
        self.env.daemon.add_learned("lobste.rs", "Lobsters", locked=False)
        self.env.write_state(streak=2)
        self.env.set_window(self.LOBSTERS, "Front page")
        _, r = self.tick()
        self.assertEqual((r["verdict"], r["source"], r["streak"]), ("allowed", "unlock", 0))

    def test_learned_title_patterns_skip_native_apps(self):
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.undecided_later()
        self.env.set_window("some-native-app", "notes - Lobsters")
        self.assertEqual(self.tick()[1]["verdict"], "undecided")

    def test_learned_group_overrides_a_config_group_with_the_same_id(self):
        self.env.groups["l-lobste-rs"] = {"label": "Old", "title_patterns": ["never-matches"]}
        self.env.write_config()
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.set_window("vivaldi-stable", "Some story - Lobsters - Vivaldi")
        self.assertEqual(self.tick()[1]["app"], "Lobsters")

    def test_learned_groups_follow_the_status_cache_rules(self):
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.write_state(next_vision_at=time.time() + 999)
        self.env.set_window("vivaldi-stable", "Some story - Lobsters - Vivaldi")
        w = self.watcher()
        self.assertEqual(w.tick()["verdict"], "procrastinating")
        self.env.daemon.stop()
        self.assertEqual(w.tick()["verdict"], "procrastinating")  # cached
        w._cache = (w._cache[0] - 601, w._cache[1])
        self.assertEqual(w.tick()["verdict"], "undecided")        # too old: unknown

    def test_no_status_means_no_learned_group_and_nothing_recorded(self):
        self.env.daemon.stop()
        self.env.write_state(next_vision_at=time.time() + 999)
        self.env.set_window("vivaldi-stable", "Some story - Lobsters - Vivaldi")
        self.assertEqual(self.tick()[1]["verdict"], "undecided")
        self.assertIsNone(self.env.read_state()["learned"])

    def test_missing_key_only_records(self):
        self.env.write_state()
        self.assertNotIn("learned", self.env.read_state())
        g = self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.set_window("foot", "shell")
        self.tick()
        self.assertEqual(self.env.notify_log(), [])
        self.assertEqual(self.env.read_state()["learned"], [g["id"]])

    def test_new_site_notifies_once(self):
        self.env.set_window("foot", "shell")
        w = self.watcher()
        w.tick()
        self.assertEqual(self.env.read_state()["learned"], [])  # recorded: nothing learned yet
        g = self.env.daemon.add_learned("lobste.rs", "Lobsters")
        w.tick()
        w.tick()
        self.assertEqual(self.env.notify_log(), [
            ["-u", "normal", "Focus", "Now blocked: Lobsters. Remove: focus learned remove lobste.rs"]])
        self.assertEqual(self.env.read_state()["learned"], [g["id"]])

    def test_one_notification_per_site(self):
        self.env.write_state(learned=[])
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.daemon.add_learned("tildes.net", "Tildes")
        self.env.set_window("foot", "shell")
        self.tick()
        self.assertEqual([n[3].split(".")[0] for n in self.env.notify_log()],
                         ["Now blocked: Lobsters", "Now blocked: Tildes"])

    def test_already_recorded_site_is_silent_after_restart(self):
        g = self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.write_state(learned=[g["id"]])
        self.env.set_window("foot", "shell")
        self.tick()
        self.assertEqual(self.env.notify_log(), [])

    def test_site_that_disappears_and_returns_is_not_announced_again(self):
        g = self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.write_state(learned=[g["id"]])
        self.env.set_window("foot", "shell")
        w = self.watcher()
        groups = self.env.daemon.status["groups"]
        self.env.daemon.status["groups"] = [x for x in groups if x["id"] != g["id"]]
        w.tick()
        self.env.daemon.status["groups"] = groups
        w.tick()
        self.assertEqual(self.env.notify_log(), [])

    def test_label_markup_is_escaped(self):
        self.env.write_state(learned=[])
        self.env.daemon.add_learned("a-b.example", "A&B <x>")
        self.env.set_window("foot", "shell")
        self.tick()
        self.assertEqual(self.env.notify_log()[0][3],
                         "Now blocked: A&amp;B &lt;x&gt;. Remove: focus learned remove a-b.example")

    def test_disabled_learned_group_is_neither_announced_nor_recorded(self):
        self.env.write_state(learned=[])
        self.env.daemon.add_learned("lobste.rs", "Lobsters", locked=False, enabled=False)
        self.env.set_window("foot", "shell")
        self.tick()
        self.assertEqual(self.env.notify_log(), [])
        self.assertEqual(self.env.read_state()["learned"], [])

    def test_announced_even_with_the_monitor_off(self):
        self.env.write_state(learned=[])
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.daemon.set_mode("off")
        _, r = self.tick()
        self.assertIsNone(r["verdict"])
        self.assertEqual(len(self.env.notify_log()), 1)
        self.assertEqual(len(self.env.read_state()["learned"]), 1)

    def test_dry_run_only_reports(self):
        self.env.write_state(learned=[])
        before = self.env.state_file().read_text()
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.set_window("foot", "shell")
        _, r = self.tick(dry_run=True)
        self.assertEqual([a["action"] for a in r["actions"]], ["notify"])
        self.assertEqual(self.env.notify_log(), [])
        self.assertEqual(self.env.state_file().read_text(), before)

    def test_once_neither_notifies_nor_uses_up_the_notification(self):
        self.env.write_state(learned=[])
        self.env.daemon.add_learned("lobste.rs", "Lobsters")
        self.env.set_window("foot", "shell")
        self.tick(once=True)
        self.assertEqual(self.env.notify_log(), [])
        self.assertEqual(self.env.read_state()["learned"], [])
        self.tick()
        self.assertEqual(len(self.env.notify_log()), 1)


class ConfigReloadTests(Base):
    def test_reload_on_mtime_change(self):
        self.env.set_window("vivaldi-stable", "Lobsters - Vivaldi")
        self.env.write_state(next_vision_at=time.time() + 999)
        w = self.watcher()
        self.assertEqual(w.tick()["verdict"], "undecided")
        self.env.write_config(distraction_title_patterns=["(?i)lobsters", "([broken"], min_confidence=0.5)
        r = w.tick()
        self.assertEqual((r["verdict"], r["app"]), ("procrastinating", "Lobsters"))
        self.assertEqual(w.cfg.min_confidence, 0.5)

    def test_unparseable_reload_keeps_previous_config(self):
        w = self.watcher()
        self.env.config_path.write_text("this is [not toml")
        os.utime(self.env.config_path, (time.time() + 50, time.time() + 50))
        self.env.set_window("vivaldi-stable", "Hacker News - Vivaldi")
        self.assertEqual(w.tick()["verdict"], "procrastinating")

    def test_reload_with_remote_ollama_keeps_loopback_url(self):
        w = self.watcher()
        old = w.cfg.ollama_url
        self.env.write_config(ollama_url="http://192.0.2.7:11434", notify_after=5)
        self.env.set_window("foot", "x")
        w.tick()
        self.assertEqual(w.cfg.notify_after, 5)
        self.assertEqual(w.cfg.ollama_url, old)

    def test_cloud_models_are_refused(self):
        for model in ("qwen3-vl:235b-cloud", "gpt-oss:120b-CLOUD"):
            self.env.write_config(model=model)
            self.assertFalse(W.Watcher().initial_config(), model)
        self.env.write_config(model="qwen3.5:4b")
        w = self.watcher()
        self.env.write_config(model="qwen3-vl:235b-cloud", notify_after=4)
        self.env.set_window("foot", "x")
        w.tick()
        self.assertEqual((w.cfg.model, w.cfg.notify_after), ("qwen3.5:4b", 4))

    def test_start_refuses_remote_ollama(self):
        self.env.write_config(ollama_url="http://192.0.2.7:11434")
        w = W.Watcher()
        self.assertFalse(w.initial_config())

    def test_missing_config_uses_defaults(self):
        self.env.config_path.unlink()
        w = W.Watcher()
        self.assertTrue(w.initial_config())
        self.assertEqual(w.cfg.mode, "observe")
        self.assertEqual(w.cfg.ollama_url, "http://127.0.0.1:11434")


if __name__ == "__main__":
    unittest.main()
