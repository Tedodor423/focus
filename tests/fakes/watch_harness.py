"""Shared test harness for focus-watch: fake focusd socket, fake Ollama, fake commands, temp dirs.

Everything lives under a fresh temp dir; nothing touches the real session, real windows or
real processes.  Imported by tests/test_watch*.py (which put this directory on sys.path).
"""
import copy
import http.server
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

FAKES = Path(__file__).resolve().parent
REPO = FAKES.parent.parent
WATCH_PY = REPO / "focus" / "watch.py"
PY = sys.executable


def load_watch_module():
    """Import focus/watch.py by path (it is self-contained; this avoids depending on focus/__init__)."""
    name = "focus_watch_under_test"
    sys.dont_write_bytecode = True  # keep __pycache__ out of the (NTFS, synced) repo
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, WATCH_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------- TOML writer

def _toml_str(s: str) -> str:
    if "'" not in s and "\n" not in s:
        return "'" + s + "'"
    return json.dumps(s)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(v)


DEFAULT_GROUPS = {
    "youtube": {
        "label": "YouTube", "enabled": True, "unlockable": True,
        "url_block": ["youtube.com", "youtu.be"], "url_allow": ["music.youtube.com"],
        "webapp_classes": [r"^chrome-(www\.)?youtube\.com__"],
        "title_patterns": [r"(?i) - YouTube( - [^-]+)?$"],
    },
    "reddit": {
        "label": "Reddit", "enabled": True, "unlockable": True,
        "url_block": ["reddit.com"], "url_allow": [],
        "webapp_classes": [r"^chrome-(www\.)?reddit\.com__"],
        "title_patterns": [r"(?i)\breddit\b"],
    },
}


def render_config(watch: dict, groups: dict) -> str:
    lines = ['user = "theo"', 'mode = "light"', "", "[watch]"]
    for k, v in watch.items():
        lines.append(f"{k} = {_toml_value(v)}")
    for gid, g in groups.items():
        lines += ["", f"[groups.{gid}]"]
        for k, v in g.items():
            lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- fake focusd

class FakeDaemon:
    """Unix-socket server speaking the focusd protocol subset focus-watch uses."""

    def __init__(self, path: str, groups: dict | None = None, mode: str = "observe"):
        self.path = path
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.penalize_error: str | None = None
        self.drop_domain = False  # like focusd when the domain isn't in the last 15 min of the DNS log
        self.penalty_minutes = 30
        self._n = 0
        self.status = {
            "ok": True, "version": "0.1.0", "mode": "light", "now": time.time(), "started": time.time(),
            "day_start": time.time() - 3600,
            "groups": [self._group(gid, g) for gid, g in (groups or DEFAULT_GROUPS).items()],
            "unlocks": [], "lists": {}, "judge_calls_last_hour": 0, "penalties": [],
            "watch": {"mode": mode, "title_interval_seconds": 60, "last_seen": None, "last": None,
                      "flags_today": 0},
        }
        self.server = None

    @staticmethod
    def _group(gid, g):
        return {"id": gid, "label": g.get("label", gid), "enabled": g.get("enabled", True),
                "unlockable": True, "locked": True, "unlocked_until": None, "used_today_min": 0,
                "daily_minutes": 40, "max_minutes": 20, "cooldown_until": None,
                "webapp_classes": g.get("webapp_classes", []), "title_patterns": g.get("title_patterns", []),
                "hosts_count": 0, "url_block": g.get("url_block", []), "url_allow": g.get("url_allow", [])}

    # -- control
    def set_mode(self, mode):
        with self.lock:
            self.status["watch"]["mode"] = mode

    def unlock(self, gid):
        with self.lock:
            for g in self.status["groups"]:
                if g["id"] == gid:
                    g["locked"] = False
                    g["unlocked_until"] = time.time() + 600

    def add_learned(self, domain, label, locked=True, enabled=True):
        """A group rendered from learned.json, as focusd reports it (§13)."""
        gid = "l-" + domain.replace(".", "-")
        g = self._group(gid, {"label": label, "enabled": enabled, "url_block": [domain],
                              "webapp_classes": [rf"^chrome-(www\.)?{re.escape(domain)}__"],
                              "title_patterns": [rf"(?i)(^| - ){re.escape(label)}( - [^-]+)?$"]})
        g.update(learned=True, locked=locked, hosts_count=2)
        with self.lock:
            self.status["groups"].append(g)
        return g

    def add_penalty(self, pid_="p00000001", **kw):
        rec = {"id": pid_, "since": time.time(), "until": time.time() + 1800, "domain": None, "app": "X",
               "title": "", "description": "", "source": "title", "reason": ""}
        rec.update(kw)
        with self.lock:
            self.status["penalties"].append(rec)
        return rec

    def end_penalty(self, pid_):
        with self.lock:
            self.status["penalties"] = [p for p in self.status["penalties"] if p["id"] != pid_]

    def ops(self, op):
        with self.lock:
            return [r for r in self.requests if r.get("op") == op]

    # -- protocol
    def respond(self, req):
        op = req.get("op")
        with self.lock:
            if op == "status":
                s = copy.deepcopy(self.status)
                s["now"] = time.time()
                return s
            if op == "observe":
                self.status["watch"]["last_seen"] = time.time()
                return {"ok": True}
            if op == "penalize":
                if self.penalize_error:
                    return {"ok": False, "error": self.penalize_error, "message": "fake refusal"}
                self._n += 1
                now = time.time()
                rec = {"id": "p%08x" % (0xa0000000 + self._n), "since": now,
                       "until": now + self.penalty_minutes * 60,
                       "domain": None if self.drop_domain else req.get("domain"),
                       "app": req.get("app"), "title": req.get("title"), "description": req.get("description"),
                       "source": req.get("source"), "reason": req.get("reason")}
                self.status["penalties"].append(rec)
                return {"ok": True, "penalty": rec}
        return {"ok": False, "error": "unknown_op", "message": op}

    def start(self):
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline(65536)
                try:
                    req = json.loads(line)
                except ValueError:
                    resp = {"ok": False, "error": "bad_request"}
                else:
                    with outer.lock:
                        outer.requests.append(req)
                    resp = outer.respond(req)
                self.wfile.write(json.dumps(resp).encode() + b"\n")

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------------------- fake Ollama

class FakeOllama:
    """HTTP server on 127.0.0.1 answering /api/chat like Ollama does with `format` + stream:false.

    behaviour: "ok" | "http500" | "badjson" | "slow" | "redirect"
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.reply = {"app": "Hacker News", "domain": "news.ycombinator.com",
                      "description": "A list of tech news links.", "verdict": "procrastinating",
                      "confidence": 0.9}
        self.behaviour = "ok"
        self.delay = 3.0
        self.server = None
        self.url = None

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                try:
                    data = json.loads(body)
                except ValueError:
                    data = {"_raw": body[:100].decode("latin1")}
                with outer.lock:
                    outer.requests.append({"path": self.path, "body": data})
                    behaviour, reply, delay = outer.behaviour, dict(outer.reply), outer.delay
                if behaviour == "slow":
                    time.sleep(delay)
                if behaviour == "http500":
                    self.send_error(500, "boom")
                    return
                if behaviour == "redirect":
                    self.send_response(307)
                    self.send_header("Location", "http://192.0.2.1/api/chat")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                content = "this is not json" if behaviour == "badjson" else json.dumps(reply)
                out = json.dumps({"model": data.get("model"), "done": True,
                                  "message": {"role": "assistant", "content": content},
                                  "load_duration": 1_000_000, "eval_count": 40}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# ----------------------------------------------------------------------------- environment

def _cmd(script: str) -> str:
    return f"{shlex.quote(PY)} -I {shlex.quote(str(FAKES / script))}"


class WatchEnv:
    """A complete fake world for one test: dirs, env vars, fake daemon + Ollama, scenario files."""

    def __init__(self, mode="observe", watch=None, groups=None, start_daemon=True):
        self.tmp = Path(tempfile.mkdtemp(prefix="focus-watch-test-"))
        self.state_home = self.tmp / "state"
        self.runtime = self.tmp / "runtime"
        self.power = self.tmp / "power"
        self.logs = self.tmp / "logs"
        for d in (self.state_home, self.runtime, self.power / "BAT0", self.logs):
            d.mkdir(parents=True)
        self.runtime.chmod(0o700)
        self.set_battery(100, "Full")
        self.config_path = self.tmp / "config.toml"
        self.socket_path = str(self.tmp / "focusd.sock")
        self.hypr_state = self.tmp / "hypr.json"
        self.groups = copy.deepcopy(groups if groups is not None else DEFAULT_GROUPS)
        self.ollama = FakeOllama().start()
        self.watch = {"mode": mode, "title_interval_seconds": 60, "vision_interval_seconds": [180, 420],
                      "vision_min_battery_percent": 15, "ollama_url": self.ollama.url, "model": "qwen3.5:4b",
                      "image_max_width": 1024, "num_ctx": 4096, "vision_timeout_seconds": 10,
                      "min_confidence": 0.8, "notify_after": 2, "enforce_after": 3, "penalty_minutes": 30,
                      "max_active_penalties": 10,
                      "browser_classes": ["^vivaldi", "^chromium$", "^firefox", "^google-chrome"],
                      "freeze_classes": ["(?i)^minecraft", "^steam_app_"],
                      "never_touch_classes": ["^Alacritty$", "^foot$", "^kitty$", r"^com\.mitchellh\.ghostty$",
                                              "^code", "^obsidian$"],
                      "work_classes": ["^org\\.pwmt\\.zathura$"],
                      "work_title_patterns": ["(?i)arxiv", r"(?i)\.pdf\b"],
                      "distraction_title_patterns": ["(?i)Hacker News", "(?i) - Netflix"]}
        if watch:
            self.watch.update(watch)
        self.write_config()
        self.daemon = FakeDaemon(self.socket_path, self.groups, mode=mode)
        if start_daemon:
            self.daemon.start()
        self.hypr = {"activewindow": {}, "clients": [],
                     "monitors": [{"name": "eDP-1", "solitaryBlockedBy": ["WINDOWED", "CANDIDATE"]}]}
        self.save_hypr()
        self.procs: list[subprocess.Popen] = []
        self.environ = {
            "FOCUS_CONFIG": str(self.config_path),
            "FOCUS_SOCKET": self.socket_path,
            "FOCUS_HYPRCTL": _cmd("watch_hyprctl.py"),
            "FOCUS_GRIM": _cmd("watch_grim.py"),
            "FOCUS_NOTIFY": _cmd("watch_notify.py"),
            "FOCUS_POWER_SUPPLY": str(self.power),
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "FAKE_HYPR_STATE": str(self.hypr_state),
            "FAKE_HYPR_LOG": str(self.logs / "hyprctl.jsonl"),
            "FAKE_GRIM_LOG": str(self.logs / "grim.jsonl"),
            "FAKE_NOTIFY_LOG": str(self.logs / "notify.jsonl"),
            "FAKE_GRIM_SIZE": "1600x1000",
        }

    # -- config / scenario
    def write_config(self, bump=True, **watch):
        self.watch.update(watch)
        self.config_path.write_text(render_config(self.watch, self.groups))
        if bump:  # make sure the mtime differs even on coarse-grained filesystems
            t = time.time() + getattr(self, "_bump", 0)
            self._bump = getattr(self, "_bump", 0) + 2
            os.utime(self.config_path, (t, t))

    def save_hypr(self):
        self.hypr_state.write_text(json.dumps(self.hypr))

    def load_hypr(self):
        return json.loads(self.hypr_state.read_text())

    def set_window(self, cls, title, address="0x55aa00001000", pid=4242, ws=(3, "3"), at=(12, 38),
                   size=(1184, 1075), extra_clients=()):
        win = {"address": address, "class": cls, "title": title, "pid": pid, "at": list(at), "size": list(size),
               "workspace": {"id": ws[0], "name": ws[1]}, "mapped": True, "hidden": False}
        self.hypr["activewindow"] = win
        self.hypr["clients"] = [win, *extra_clients]
        self.save_hypr()
        return win

    def set_battery(self, capacity, status):
        (self.power / "BAT0" / "capacity").write_text(f"{capacity}\n")
        (self.power / "BAT0" / "status").write_text(f"{status}\n")

    def _jsonl(self, name):
        p = self.logs / name
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def hypr_log(self):
        return [e["argv"] for e in self._jsonl("hyprctl.jsonl")]

    def dispatches(self):
        return [a[1] for a in self.hypr_log() if a and a[0] == "dispatch"]

    def grim_log(self):
        return self._jsonl("grim.jsonl")

    def notify_log(self):
        return [e["argv"] for e in self._jsonl("notify.jsonl")]

    def runtime_files(self):
        return sorted(os.listdir(self.runtime))

    def state_file(self):
        return self.state_home / "focus" / "watch.json"

    def read_state(self):
        return json.loads(self.state_file().read_text())

    def write_state(self, **d):
        p = self.state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        base = {"version": 1, "streak": 0, "next_vision_at": 0, "hidden": []}
        base.update(d)
        p.write_text(json.dumps(base))
        p.chmod(0o600)

    # -- processes the test owns
    def start_sleep(self, exe="/bin/sleep"):
        p = subprocess.Popen([exe, "300"], stdin=subprocess.DEVNULL)
        self.procs.append(p)
        for _ in range(100):  # wait until it has exec'd
            try:
                if Path(f"/proc/{p.pid}/comm").read_text().strip() == Path(exe).name[:15]:
                    break
            except OSError:
                pass
            time.sleep(0.01)
        return p

    @staticmethod
    def proc_state(pid):
        data = Path(f"/proc/{pid}/stat").read_text()
        return data[data.rfind(")") + 2:].split()[0]

    @staticmethod
    def proc_start(pid):
        data = Path(f"/proc/{pid}/stat").read_text()
        return int(data[data.rfind(")") + 2:].split()[19])

    def wait_state(self, pid, want, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.proc_state(pid) in want:
                return True
            time.sleep(0.02)
        return False

    def close(self):
        for p in self.procs:
            try:
                p.send_signal(signal.SIGCONT)
                p.kill()
                p.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        self.daemon.stop()
        self.ollama.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
