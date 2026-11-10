"""Tests for dnslog/focus-dnslog (builder D).

Canned `resolvectl monitor --json=short` lines (shape from the varlink interface
io.systemd.Resolve.Monitor.SubscribeQueryResults, systemd 262) are fed through a
fake monitor command; nothing here needs root or touches /var/log.
"""

import datetime
import importlib.machinery
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "dnslog", "focus-dnslog")


def load_module():
    loader = importlib.machinery.SourceFileLoader("focus_dnslog", SCRIPT)
    spec = importlib.util.spec_from_loader("focus_dnslog", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    loader.exec_module(mod)
    return mod


dnslog = load_module()


def q(name, qtype=1):
    return {"class": 1, "type": qtype, "name": name}


def a_rr(name, ip):
    return {"rr": {"key": q(name, 1), "address": [int(x) for x in ip.split(".")]}, "raw": "AAAA", "ifindex": 3}


def aaaa_rr(name, packed16):
    return {"rr": {"key": q(name, 28), "address": list(packed16)}, "raw": "AAAA"}


def cname_rr(name, target):
    return {"rr": {"key": q(name, 5), "name": target}, "raw": "AAAA"}


SUCCESS_A = {"state": "success", "question": [q("Example.COM.")], "answer": [a_rr("example.com", "93.184.215.14")]}
DUAL = {
    "state": "success",
    "question": [q("reddit.com", 1), q("reddit.com", 28)],
    "answer": [a_rr("reddit.com", "0.0.0.0"), aaaa_rr("reddit.com", bytes(16))],
}
CNAME_CHAIN = {
    "state": "success",
    "question": [q("www.github.com")],
    "collectedQuestions": [q("www.github.com"), q("github.com")],
    "answer": [cname_rr("www.github.com", "github.com."), a_rr("github.com", "140.82.121.4")],
}
NXDOMAIN = {"state": "rcode-failure", "rcode": 3, "question": [q("nope.invalid")]}
READY = {"ready": True}

CANNED = [READY, SUCCESS_A, "this is not json", DUAL, NXDOMAIN, CNAME_CHAIN, [1, 2], {"state": "success"}]


def canned_lines(items=CANNED):
    return [x if isinstance(x, str) else json.dumps(x, separators=(",", ":")) for x in items]


class ParseTests(unittest.TestCase):
    def test_simple_a(self):
        recs = dnslog.parse_line(json.dumps(SUCCESS_A), now=1000.0)
        self.assertEqual(recs, [{"t": 1000.0, "name": "example.com", "type": "A", "addrs": ["93.184.215.14"],
                                 "blocked": False}])

    def test_dual_stack_blocked_gives_two_lines(self):
        recs = dnslog.parse_line(json.dumps(DUAL), now=5.0)
        self.assertEqual([(r["name"], r["type"], r["addrs"], r["blocked"]) for r in recs],
                         [("reddit.com", "A", ["0.0.0.0"], True), ("reddit.com", "AAAA", ["::"], True)])

    def test_cname_chain(self):
        (rec,) = dnslog.parse_line(json.dumps(CNAME_CHAIN), now=1.0)
        self.assertEqual(rec["name"], "www.github.com")
        self.assertEqual(rec["cnames"], ["github.com"])
        self.assertEqual(rec["addrs"], ["140.82.121.4"])

    def test_skips_ready_failures_and_garbage(self):
        for item in (READY, NXDOMAIN, "garbage", "", "[1,2]", {"state": "success"},
                     {"state": "success", "question": "x"}, {"state": "success", "question": [{"type": 1}]}):
            line = item if isinstance(item, str) else json.dumps(item)
            self.assertEqual(dnslog.parse_line(line), [], line)

    def test_all_states_keeps_failures_with_state(self):
        (rec,) = dnslog.parse_line(json.dumps(NXDOMAIN), now=1.0, all_states=True)
        self.assertEqual((rec["name"], rec["state"]), ("nope.invalid", "rcode-failure"))

    def test_json_seq_framing_and_bytes(self):
        recs = dnslog.parse_line(("\x1e" + json.dumps(SUCCESS_A) + "\n").encode())
        self.assertEqual(recs[0]["name"], "example.com")

    def test_unknown_type_and_https(self):
        (rec,) = dnslog.parse_line(json.dumps({"state": "success", "question": [q("x.org", 65)]}))
        self.assertEqual(rec["type"], "HTTPS")
        (rec,) = dnslog.parse_line(json.dumps({"state": "success", "question": [q("x.org", 4242)]}))
        self.assertEqual(rec["type"], "TYPE4242")

    def test_record_has_contract_fields(self):
        (rec,) = dnslog.parse_line(json.dumps(SUCCESS_A))
        self.assertIsInstance(rec["t"], float)
        self.assertAlmostEqual(rec["t"], time.time(), delta=5)
        self.assertEqual(rec["name"], rec["name"].lower())
        self.assertFalse(rec["name"].endswith("."))


class WriterTests(unittest.TestCase):
    def test_day_files_modes_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "var", "log", "focus", "dns")
            w = dnslog.DayWriter(out, keep_days=30)
            w.ensure_dir()
            old = os.path.join(out, "2000-01-01.jsonl")
            keep_other = os.path.join(out, "notes.txt")
            for p in (old, keep_other):
                open(p, "w").close()
            t1 = datetime.datetime(2026, 11, 10, 23, 59, 59).timestamp()
            t2 = datetime.datetime(2026, 11, 11, 0, 0, 1).timestamp()
            w.write({"t": t1, "name": "a.com", "type": "A"})
            w.write({"t": t2, "name": "b.com", "type": "A"})
            w.close()
            self.assertTrue(os.path.exists(os.path.join(out, "2026-11-10.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(out, "2026-11-11.jsonl")))
            self.assertFalse(os.path.exists(old), "old day file should be pruned")
            self.assertTrue(os.path.exists(keep_other), "non-day files are never touched")
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(out)).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(out, "2026-11-10.jsonl")).st_mode), 0o640)
            with open(os.path.join(out, "2026-11-11.jsonl")) as f:
                self.assertEqual(json.loads(f.readline())["name"], "b.com")

    def test_prune_keep_zero_keeps_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = dnslog.DayWriter(tmp, keep_days=0)
            open(os.path.join(tmp, "1999-01-01.jsonl"), "w").close()
            self.assertEqual(w.prune(), [])
            self.assertTrue(os.path.exists(os.path.join(tmp, "1999-01-01.jsonl")))


class ProcessTests(unittest.TestCase):
    """Run the real script against a fake monitor command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.out = os.path.join(self.root, "dns")
        self.feed = os.path.join(self.root, "feed.jsonl")
        with open(self.feed, "w") as f:
            f.write("\n".join(canned_lines()) + "\n")
        self.fake = os.path.join(self.root, "fake_resolvectl.py")
        with open(self.fake, "w") as f:
            f.write(textwrap.dedent("""\
                import sys, time
                for line in open(sys.argv[1]):
                    sys.stdout.write(line); sys.stdout.flush()
                if len(sys.argv) > 2:
                    time.sleep(float(sys.argv[2]))
                """))

    def env(self):
        env = dict(os.environ)
        env["FOCUS_ROOT"] = self.root  # never chown, never real paths
        env.pop("FOCUS_DNSLOG_COMMAND", None)
        return env

    def records(self):
        recs = []
        for fn in sorted(os.listdir(self.out)):
            with open(os.path.join(self.out, fn)) as f:
                recs += [json.loads(line) for line in f]
        return recs

    def test_once_with_fake_command(self):
        cmd = f"{sys.executable} -I {self.fake} {self.feed}"
        r = subprocess.run([sys.executable, "-I", SCRIPT, "--once", "--outdir", self.out, "--command", cmd],
                           env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        recs = self.records()
        self.assertEqual([(x["name"], x["type"]) for x in recs],
                         [("example.com", "A"), ("reddit.com", "A"), ("reddit.com", "AAAA"), ("www.github.com", "A")])
        for x in recs:
            self.assertIn("t", x)
        self.assertIn("stopped after 4 records", r.stderr)

    def test_default_outdir_uses_focus_root(self):
        cmd = f"{sys.executable} -I {self.fake} {self.feed}"
        r = subprocess.run([sys.executable, "-I", SCRIPT, "--once", "--command", cmd],
                           env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.listdir(os.path.join(self.root, "var", "log", "focus", "dns")))

    def test_stdin_mode(self):
        with open(self.feed) as f:
            r = subprocess.run([sys.executable, "-I", SCRIPT, "--stdin", "--outdir", self.out],
                               stdin=f, env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.records()), 4)

    def test_restarts_command_after_exit(self):
        cmd = f"{sys.executable} -I {self.fake} {self.feed}"
        p = subprocess.Popen([sys.executable, "-I", SCRIPT, "--outdir", self.out, "--command", cmd],
                             env=self.env(), stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if os.path.isdir(self.out) and len(self.records()) >= 8:
                    break
                time.sleep(0.2)
            self.assertGreaterEqual(len(self.records()), 8, "the fake monitor should have run twice")
        finally:
            p.send_signal(signal.SIGTERM)
            _, err = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        self.assertIn("restarting in", err)

    def test_sigterm_stops_child_and_exits_zero(self):
        cmd = f"{sys.executable} -I {self.fake} {self.feed} 60"
        p = subprocess.Popen([sys.executable, "-I", SCRIPT, "--outdir", self.out, "--command", cmd],
                             env=self.env(), stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not (os.path.isdir(self.out) and len(self.records()) >= 4):
                time.sleep(0.2)
            self.assertEqual(len(self.records()), 4)
        finally:
            p.send_signal(signal.SIGTERM)
            p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        # The fake child (sleeping 60 s) must be gone too.
        r = subprocess.run(["pgrep", "-f", self.feed], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "", "monitor child still running")

    def test_missing_command_keeps_retrying_until_stopped(self):
        p = subprocess.Popen([sys.executable, "-I", SCRIPT, "--outdir", self.out,
                              "--command", os.path.join(self.root, "no-such-resolvectl")],
                             env=self.env(), stderr=subprocess.PIPE, text=True)
        time.sleep(1.5)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0)
        self.assertIn("cannot start", err)


if __name__ == "__main__":
    unittest.main()
