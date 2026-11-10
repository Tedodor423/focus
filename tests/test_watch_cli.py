"""End-to-end tests of the `focus-watch` command line (focus/watch.py run as a script).

Runs `python3 -I focus/watch.py …` against the same fakes as test_watch.py.  Signals go only
to the focus-watch subprocesses and `sleep` processes started here.
"""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))
import watch_harness as H  # noqa: E402

YT_WEBAPP = "chrome-www.youtube.com__-Default"


class CliBase(unittest.TestCase):
    mode = "observe"

    def setUp(self):
        self.env = H.WatchEnv(mode=self.mode)
        self.addCleanup(self.env.close)
        self.children = []
        self.addCleanup(self._kill_children)

    def _kill_children(self):
        for c in self.children:
            if c.poll() is None:
                c.kill()
                c.wait(timeout=5)
            for f in (c.stdout, c.stderr):
                if f:
                    f.close()

    def environ(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("FOCUS_", "FAKE_"))}
        e.update(self.env.environ)
        e.update(extra)
        return e

    def run_watch(self, *args, timeout=60, **extra):
        return subprocess.run([H.PY, "-I", str(H.WATCH_PY), *args], env=self.environ(**extra),
                              capture_output=True, text=True, timeout=timeout)

    def start_watch(self, *args, **extra):
        p = subprocess.Popen([H.PY, "-I", str(H.WATCH_PY), *args], env=self.environ(**extra),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(p)
        return p

    def wait_for(self, cond, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False


class OnceTests(CliBase):
    def test_once_prints_result_and_reports(self):
        self.env.set_window("foot", "nvim")
        r = self.run_watch("--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["verdict"], out["source"], out["class"], out["daemon"]), ("working", "title", "foot", "ok"))
        self.assertEqual(len(self.env.daemon.ops("status")), 1)
        [obs] = self.env.daemon.ops("observe")
        self.assertEqual(obs["verdict"], "working")
        self.assertEqual(self.env.read_state()["streak"], 0)
        self.assertEqual(self.env.state_file().stat().st_mode & 0o777, 0o600)

    def test_once_runs_vision_and_cleans_up(self):
        self.env.set_window("vivaldi-stable", "Some page - Vivaldi")
        self.env.write_state(next_vision_at=time.time() + 300)  # --once checks now regardless of the timer
        r = self.run_watch("--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["verdict"], out["source"], out["domain"]),
                         ("procrastinating", "vision", "news.ycombinator.com"))
        self.assertEqual(self.env.runtime_files(), [])

    def test_no_report(self):
        self.env.set_window("foot", "nvim")
        r = self.run_watch("--once", "--no-report")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env.daemon.ops("observe"), [])

    def test_daemon_unreachable(self):
        self.env.daemon.stop()
        self.env.set_window("foot", "nvim")
        r = self.run_watch("--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["daemon"], out["verdict"]), ("unreachable", "working"))
        self.assertIn("unreachable", r.stderr)

    def test_refuses_non_loopback_ollama(self):
        for url in ("http://192.168.1.5:11434", "http://ollama.example.com", "http://127.0.0.1@evil.example/"):
            self.env.write_config(ollama_url=url)
            self.env.set_window("vivaldi-stable", "Some page - Vivaldi")
            r = self.run_watch("--once")
            self.assertEqual(r.returncode, 1, url)
            self.assertIn("not a loopback address", r.stderr)
            self.assertEqual(r.stdout, "")
        r = self.run_watch()  # the run loop refuses too
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.env.ollama.requests, [])
        self.assertEqual(self.env.grim_log(), [])
        self.assertEqual(self.env.daemon.requests, [])

    def test_usage_error(self):
        r = self.run_watch("--bogus")
        self.assertEqual(r.returncode, 2)


class DryRunCliTests(CliBase):
    mode = "enforce"

    def test_once_dry_run_never_enforces(self):
        p = self.env.start_sleep()
        self.env.write_state(streak=2, notified=True)
        self.env.set_window("Minecraft* 1.21", "Minecraft", pid=p.pid)
        self.env.ollama.reply = {"app": "Minecraft", "domain": "", "description": "A block game",
                                 "verdict": "procrastinating", "confidence": 0.97}
        before = self.env.state_file().read_text()
        r = self.run_watch("--once", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual((out["verdict"], out["streak"]), ("procrastinating", 3))
        self.assertEqual([a["action"] for a in out["actions"]], ["skip_enforce"])
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.notify_log(), [])
        self.assertNotEqual(self.env.proc_state(p.pid), "T")
        self.assertEqual(self.env.state_file().read_text(), before)
        self.assertEqual(len(self.env.daemon.ops("observe")), 1)  # observe still sent

    def test_once_never_enforces(self):
        self.env.write_state(streak=2, notified=True)
        self.env.set_window(YT_WEBAPP, "Home")
        r = self.run_watch("--once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env.daemon.ops("penalize"), [])
        self.assertEqual(self.env.dispatches(), [])
        self.assertEqual(self.env.notify_log(), [])

    def test_once_dry_run_no_report(self):
        self.env.set_window(YT_WEBAPP, "Home")
        r = self.run_watch("--once", "--dry-run", "--no-report")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.env.daemon.ops("observe"), [])
        self.assertFalse(self.env.state_file().exists())

    def test_dry_run_loop_prints_ticks(self):
        self.env.write_config(title_interval_seconds=5)
        self.env.set_window(YT_WEBAPP, "Home")
        p = self.start_watch("--dry-run")
        line = p.stdout.readline()
        self.assertEqual(json.loads(line)["verdict"], "procrastinating")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=10), 0)
        self.assertEqual(self.env.dispatches(), [])


class LoopTests(CliBase):
    mode = "enforce"

    def jail_sleep(self):
        p = self.env.start_sleep()
        os.kill(p.pid, signal.SIGSTOP)
        self.assertTrue(self.env.wait_state(p.pid, "T"))
        client = {"address": "0x55aa0000aaaa", "class": "steam_app_7", "title": "Game", "pid": p.pid,
                  "at": [0, 0], "size": [10, 10], "workspace": {"id": -98, "name": "special:focusjail"}}
        entry = {"penalty_id": "p00000001", "address": "0x55aa0000aaaa", "class": "steam_app_7", "title": "Game",
                 "workspace": "6", "workspace_id": 6, "pid": p.pid, "pid_start": self.env.proc_start(p.pid),
                 "frozen": True, "since": time.time()}
        self.env.set_window("foot", "shell", address="0x55aa00000001", extra_clients=[client])
        self.env.write_state(hidden=[entry])
        self.env.daemon.add_penalty("p00000001")  # still active: only shutdown may restore it
        return p

    def test_sigterm_restores_everything(self):
        sl = self.jail_sleep()
        w = self.start_watch()
        self.assertTrue(self.wait_for(lambda: self.env.daemon.ops("observe")), "no first tick")
        self.assertEqual(self.env.proc_state(sl.pid), "T")
        # a second, non-dry-run instance is refused while the loop runs; a dry run is fine
        r = self.run_watch("--once")
        self.assertEqual(r.returncode, 1)
        self.assertIn("another focus-watch is running", r.stderr)
        r = self.run_watch("--once", "--dry-run", "--no-report")
        self.assertEqual(r.returncode, 0, r.stderr)
        w.send_signal(signal.SIGTERM)
        self.assertEqual(w.wait(timeout=15), 0)
        self.assertTrue(self.env.wait_state(sl.pid, "SR"), "frozen app must be resumed on shutdown")
        self.assertEqual(self.env.dispatches(), [
            'hl.dsp.window.move({ workspace = "6", follow = false, window = "address:0x55aa0000aaaa" })'])
        self.assertEqual(self.env.read_state()["hidden"], [])

    def test_restore_flag(self):
        sl = self.jail_sleep()
        self.env.write_config(ollama_url="http://192.0.2.1:11434")  # a refused config must not block restoring
        r = self.run_watch("--restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.env.wait_state(sl.pid, "SR"))
        self.assertEqual(self.env.dispatches(), [
            'hl.dsp.window.move({ workspace = "6", follow = false, window = "address:0x55aa0000aaaa" })'])
        self.assertEqual(self.env.read_state()["hidden"], [])
        self.assertEqual(self.env.daemon.requests, [])  # works without focusd

    def test_restore_flag_keeps_what_it_cannot_restore(self):
        sl = self.jail_sleep()
        self.env.hypr["fail"] = ["clients"]
        self.env.save_hypr()
        r = self.run_watch("--restore")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.env.wait_state(sl.pid, "SR"))  # resumed even so
        self.assertEqual(len(self.env.read_state()["hidden"]), 1)

    def test_sigint_also_restores(self):
        sl = self.jail_sleep()
        w = self.start_watch()
        self.assertTrue(self.wait_for(lambda: self.env.daemon.ops("observe")))
        w.send_signal(signal.SIGINT)
        self.assertEqual(w.wait(timeout=15), 0)
        self.assertTrue(self.env.wait_state(sl.pid, "SR"))

    def test_sigterm_during_vision_call(self):
        self.env.ollama.behaviour, self.env.ollama.delay = "slow", 20
        self.env.write_config(vision_timeout_seconds=60)
        self.env.set_window("vivaldi-stable", "Some page - Vivaldi")
        w = self.start_watch()
        self.assertTrue(self.wait_for(lambda: self.env.ollama.requests), "vision call never started")
        t0 = time.time()
        w.send_signal(signal.SIGTERM)
        self.assertEqual(w.wait(timeout=15), 0)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(self.env.runtime_files(), [])
        self.assertEqual(self.env.daemon.ops("penalize"), [])

    def test_loop_survives_daemon_restart_and_reports_again(self):
        self.env.write_config(title_interval_seconds=5)
        self.env.set_window("foot", "shell")
        self.env.daemon.stop()
        w = self.start_watch()
        time.sleep(1.0)
        self.assertIsNone(w.poll(), "monitor must keep running without the daemon")
        self.env.daemon = H.FakeDaemon(self.env.socket_path, self.env.groups, mode="enforce").start()
        self.assertTrue(self.wait_for(lambda: self.env.daemon.ops("observe"), timeout=10))
        w.send_signal(signal.SIGTERM)
        self.assertEqual(w.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
