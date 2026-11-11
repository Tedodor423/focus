"""Test harness: runs the real focusd as this (non-root) user under a
FOCUS_ROOT temp dir, with the fake judge. Imported by tests/test_daemon_*.py."""

import getpass
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FAKE = os.path.join(HERE, "daemon_fake_claude.py")

HOSTS = (b"# Static table lookup for hostnames.\n"
         b"# See hosts(5) for details.\n"
         b"127.0.0.1        localhost\n"
         b"::1              localhost\n"
         b"10.0.0.5\tnas.lan nas   # trailing comment\n")

CONFIG = r'''
user = "{user}"
mode = "light"
day_starts_at = "04:00"
reconcile_seconds = {reconcile}

[limits]
max_unlock_minutes = 20
daily_minutes = 40
deny_cooldown_minutes = 5
judge_calls_per_hour = {calls}
max_followups = 2

[judge]
command = ["{python}", "{fake}", "{script}"]
model = "sonnet"
timeout_seconds = {timeout}
policy_file = "/etc/focus/judge.md"
context_files = ["/etc/focus/context.md", "/etc/focus/missing.md"]

[browser]
policy_dirs = ["/etc/chromium/policies/managed", "/etc/vivaldi/policies/managed"]
disable_doh = true
force_google_safesearch = true

[safety]
never_block = ["google.com", "github.com", "example.ac.uk", "docs.example.net"]

[lists.porn]
url = "https://127.0.0.1:9/list.txt"
refresh_days = 7
min_entries = 2
max_entries = 100

[groups.youtube]
label = "YouTube"
max_minutes = 15
hosts = []
url_block = ["youtube.com", "youtu.be"]
url_allow = ["music.youtube.com", "youtube.com/embed"]
webapp_classes = ['^chrome-(www\.)?youtube\.com__']
title_patterns = ['(?i) - YouTube( - [^-]+)?$']

[groups.reddit]
label = "Reddit"
daily_minutes = 25
hosts = ["reddit.com", "www.reddit.com", "old.reddit.com"]
url_block = ["reddit.com"]

[groups.games]
label = "Games"
unlockable = false
hosts = ["poki.com"]

[groups.porn]
label = "Porn"
unlockable = false
lists = ["porn"]

[groups.off]
label = "Disabled"
enabled = false
hosts = ["example.org"]

[watch]
mode = "{watch_mode}"
title_interval_seconds = 60
penalty_minutes = 30
max_active_penalties = 3
'''


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def tmp_root():
    # Short path: AF_UNIX socket paths are limited to 107 bytes.
    return tempfile.mkdtemp(prefix="fd", dir="/tmp")


class Daemon:
    def __init__(self, responses=None, hosts=HOSTS, **cfg):
        self.root = tmp_root()
        self.proc = None
        self.script = os.path.join(self.root, "judge-script.json")
        for d in ("etc/focus", "etc/chromium/policies/managed", "var/lib/focus"):
            os.makedirs(self.path("/" + d))
        if hosts is not None:
            with open(self.path("/etc/hosts"), "wb") as f:
                f.write(hosts)
        with open(self.path("/etc/focus/judge.md"), "w") as f:
            f.write("Be strict. Test policy.\n")
        with open(self.path("/etc/focus/context.md"), "w") as f:
            f.write("CONTEXT-MARKER: Theo has a deadline on Friday.\n")
        self.set_responses(*(responses or [{"decision": "deny", "minutes": 0, "message": "no"}]))
        self.write_config(**cfg)

    # -------------------------------------------------- files

    def path(self, p):
        return self.root + p

    def config_text(self, user=None, reconcile=30, calls=8, timeout=10, watch_mode="enforce",
                    learn=""):
        return CONFIG.format(user=user or getpass.getuser(), reconcile=reconcile, calls=calls,
                             timeout=timeout, watch_mode=watch_mode, python=sys.executable,
                             fake=FAKE, script=self.script) + learn

    def write_config(self, text=None, **cfg):
        with open(self.path("/etc/focus/config.toml"), "w") as f:
            f.write(text if text is not None else self.config_text(**cfg))

    def set_responses(self, *steps):
        with open(self.script, "w") as f:
            json.dump({"responses": list(steps)}, f)
        try:
            os.unlink(self.script + ".calls.jsonl")
        except FileNotFoundError:
            pass

    def judge_calls(self):
        try:
            with open(self.script + ".calls.jsonl") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def hosts(self):
        with open(self.path("/etc/hosts"), "rb") as f:
            return f.read()

    def policy(self, browser="chromium"):
        with open(self.path(f"/etc/{browser}/policies/managed/focus.json")) as f:
            return json.load(f)

    def state(self):
        with open(self.path("/var/lib/focus/state.json")) as f:
            return json.load(f)

    def read_json(self, name):
        with open(self.path(f"/var/lib/focus/{name}")) as f:
            return json.load(f)

    def write_json(self, name, data):
        with open(self.path(f"/var/lib/focus/{name}"), "w") as f:
            json.dump(data, f)

    def write_state(self, data):
        with open(self.path("/var/lib/focus/state.json"), "w") as f:
            json.dump(data, f)

    def write_events(self, events):
        with open(self.path("/var/lib/focus/events.jsonl"), "a") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def events(self, type_=None):
        evs = []
        try:
            with open(self.path("/var/lib/focus/events.jsonl")) as f:
                for line in f:
                    try:
                        evs.append(json.loads(line))
                    except ValueError:
                        pass      # torn lines written by tests
        except FileNotFoundError:
            return []
        return [e for e in evs if type_ is None or e["type"] == type_]

    def wait_for(self, pred, timeout=5.0):
        return wait_for(pred, timeout)

    def wait_event(self, type_, timeout=5.0, **match):
        def find():
            return [e for e in self.events(type_)
                    if all(e.get(k) == v for k, v in match.items())]
        return self.wait_for(find, timeout)

    # -------------------------------------------------- process

    @property
    def sock(self):
        return self.path("/run/focusd/focusd.sock")

    def start(self, wait=True):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FOCUS_ROOT": self.root,
               "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        self.log = open(os.path.join(self.root, "focusd.log"), "ab")
        code = f"import sys; sys.path.insert(0, {REPO!r}); from focus.daemon import main; main()"
        self.proc = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=self.root,
                                     stdout=self.log, stderr=self.log)
        if wait:
            self.wait_ready()
        return self

    def wait_ready(self, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise AssertionError(f"focusd exited with {self.proc.returncode}: {self.stderr()}")
            try:
                if self.request({"op": "status"}, timeout=2).get("ok"):
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError("focusd did not come up: " + self.stderr())

    def stderr(self):
        try:
            with open(os.path.join(self.root, "focusd.log"), "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def raw(self, data: bytes, timeout=30.0, eof=False) -> bytes:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock)
            s.sendall(data)
            if eof:
                s.shutdown(socket.SHUT_WR)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return buf
        finally:
            s.close()

    def request(self, obj, timeout=30.0):
        return json.loads(self.raw(json.dumps(obj).encode() + b"\n", timeout))

    def send(self, sig):
        self.proc.send_signal(sig)

    def stop(self, sig=signal.SIGTERM, timeout=10.0):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                raise AssertionError("focusd did not stop")
        if getattr(self, "log", None):
            self.log.close()
        return self.proc.returncode if self.proc else None

    def cleanup(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if getattr(self, "log", None):
            self.log.close()
        shutil.rmtree(self.root, ignore_errors=True)


def fake_dnslog(root, entries, day=None):
    """Write DNS-log lines under FOCUS_ROOT (today's local file by default)."""
    d = root + "/var/log/focus/dns"
    os.makedirs(d, exist_ok=True)
    day = day or time.strftime("%Y-%m-%d")
    with open(os.path.join(d, f"{day}.jsonl"), "a") as f:
        for t, name in entries:
            f.write(json.dumps({"t": t, "name": name, "type": "A"}) + "\n")
