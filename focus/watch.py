"""focus-watch: the screen monitor of `focus` (docs/ARCHITECTURE.md §12).

Runs as the desktop user inside the Hyprland session.  Every tick it looks at
the active window, classifies it (window title rules first, a local vision
model on a screenshot of that window only when the title is undecided),
reports the verdict to focusd (`observe`), and in nudge/enforce mode escalates:
a notification, then a penalty (`penalize`) plus hiding/freezing the window.

Self-contained on purpose: stdlib only (Pillow is used if importable), and no
imports from the other `focus` modules.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import html
import io
import ipaddress
import json
import os
import random
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
MODES = ("off", "observe", "nudge", "enforce")
VERDICTS = ("working", "procrastinating", "unclear", "allowed", "idle", "undecided", "error")
JAIL = "special:focusjail"
STR_MAX = 300
STATUS_CACHE_SECONDS = 600   # use the last good status this long while focusd is unreachable
STALE_TEMP_SECONDS = 120     # focus-watch-* images older than this are leftovers of a crash
WEBAPP_RE = re.compile(r"^chrome-.*__")   # Chromium --app windows (Omarchy web apps)

DEFAULT_WATCH = {
    "mode": "observe",
    "title_interval_seconds": 60,
    "vision_interval_seconds": [180, 420],
    "vision_min_battery_percent": 15,
    "ollama_url": "http://127.0.0.1:11434",
    "model": "qwen3.5:4b",
    "image_max_width": 1024,
    "num_ctx": 4096,
    "vision_timeout_seconds": 60,
    "min_confidence": 0.8,
    "notify_after": 2,
    "enforce_after": 3,
    "penalty_minutes": 30,
    "max_active_penalties": 10,
    "browser_classes": ["^vivaldi", "^chromium$", "^firefox", "^google-chrome"],
    "freeze_classes": [],   # off by default: Hyprland's ANR dialog would offer "Terminate" on a stopped app
    "never_touch_classes": [
        "^Alacritty$", "^foot$", "^kitty$", r"^com\.mitchellh\.ghostty$", "^code", "^obsidian$",
    ],
    "work_classes": [],
    "work_title_patterns": [],
    "distraction_title_patterns": [],
}

# (key, minimum, maximum, integer?)
NUMERIC_KEYS = {
    "title_interval_seconds": (5, 3600, False),
    "vision_min_battery_percent": (0, 100, False),
    "image_max_width": (64, 8192, True),
    "num_ctx": (512, 262144, True),
    "vision_timeout_seconds": (1, 600, False),
    "min_confidence": (0.0, 1.0, False),
    "notify_after": (1, 1000, True),
    "enforce_after": (1, 1000, True),
    "penalty_minutes": (1, 1440, True),
    "max_active_penalties": (1, 1000, True),
}
REGEX_KEYS = (
    "browser_classes", "freeze_classes", "never_touch_classes",
    "work_classes", "work_title_patterns", "distraction_title_patterns",
)
GROUP_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)
# Processes that must never get SIGSTOP, whatever their window class says.
NEVER_FREEZE_COMM = re.compile(
    r"(?i)^(chrom|chrome|google-chrome|vivaldi|firefox|hyprland|quickshell|qs$|systemd|"
    r"dbus|pipewire|wireplumber|xdg-|foot|alacritty|kitty|ghostty|code|obsidian|focus)"
)

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "description": "the website's domain: read it from the address bar when visible, otherwise "
                           "identify the site from its content and branding (e.g. news.ycombinator.com, "
                           "youtube.com); empty string if it is not a website or the site is unknown",
        },
        "app": {"type": "string",
                "description": "name of the website or app in focus, e.g. YouTube, GitHub, Hacker News, Minecraft "
                               "(the site, not the browser)"},
        "description": {"type": "string", "description": "one sentence: what is on screen"},
        "verdict": {"type": "string", "enum": ["working", "procrastinating", "unclear"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["domain", "app", "description", "verdict", "confidence"],
}

VISION_SYSTEM = """You look at a screenshot of the focused window on Theo's laptop and decide whether he is working or procrastinating.
Work: research, scientific papers and PDFs, code, terminals, writing, documents, spreadsheets, study material, work email or work chat, documentation.
Procrastination: social media feeds (Reddit, X, Instagram, Facebook, TikTok), YouTube or other video entertainment, games, shopping, news sites and link aggregators (including tech news such as Hacker News), memes, porn.
Unclear: lock screen, empty desktop, music player alone, settings, anything you cannot judge.
Judge the main, largest content. Read visible text (titles, URLs, headings) to decide. The window class and title are given as text; use them.
domain: if an address bar is visible, copy the host name exactly as shown there. If not (fullscreen browsers hide it), identify the website from its name, logo or layout and give its usual domain, whether it is work or not: e.g. Hacker News -> news.ycombinator.com, YouTube -> youtube.com, a Reddit thread -> reddit.com, a GitHub repository -> github.com. Use "" only when the window is not a website (terminal, editor, game, desktop app) or nothing identifies the site."""


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(f"focus-watch: {msg}", file=sys.stderr, flush=True)


def trunc(value, n: int = STR_MAX) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def is_loopback_url(url) -> bool:
    """True only for http(s) URLs whose host is a loopback IP literal or `localhost`."""
    if not isinstance(url, str):
        return False
    try:
        p = urllib.parse.urlsplit(url.strip())
        p.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or p.username or p.password:
        return False
    host = p.hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_cloud_model(name) -> bool:
    """Ollama `…-cloud` models run remotely: they would upload the screenshot."""
    return not isinstance(name, str) or "cloud" in name.lower()


def normalize_domain(value) -> str:
    """Model output → bare lowercase host name without `www.`, or "" if it isn't one."""
    if not isinstance(value, str):
        return ""
    s = value.strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    for sep in ("/", "?", "#", " "):
        s = s.split(sep, 1)[0]
    s = s.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    return s if HOST_RE.match(s) else ""


def lua_str(s: str) -> str:
    out = []
    for ch in s:
        if ch in ('"', "\\") or ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\%03d" % ord(ch) if ord(ch) < 0x80 else ch)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def move_expr(address: str, workspace: str) -> str:
    if not ADDRESS_RE.match(address or ""):
        raise ValueError(f"bad window address {address!r}")
    return (f"hl.dsp.window.move({{ workspace = {lua_str(workspace)}, follow = false, "
            f"window = {lua_str('address:' + address)} }})")


def workspace_selector(ws_id, ws_name) -> str:
    """Selector that sends a window back to the workspace it was taken from."""
    name = ws_name if isinstance(ws_name, str) else ""
    if name == JAIL:  # never "restore" into the jail (that would retry forever)
        return "1"
    if name.startswith("special:"):
        return name
    if isinstance(ws_id, int) and ws_id >= 1:
        return str(ws_id)
    if name:
        return "name:" + name
    return "1"


_BROWSER_NAME = re.compile(r"(?i)^(vivaldi|chromium|(google )?chrome|(mozilla )?firefox|brave( browser)?)$")
_TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014|\u00b7]\s+")


def title_stem(title) -> str:
    """The site part of a window title: 'Video - YouTube - Vivaldi' -> 'youtube'; '' if only a browser name."""
    t = re.sub(r"^\(\d+\+?\)\s*", "", str(title or "").strip())
    parts = [x.strip() for x in _TITLE_SPLIT.split(t) if x.strip()]
    while parts and _BROWSER_NAME.match(parts[-1]):
        parts.pop()
    return parts[-1].casefold() if parts else ""


def command_from_env(var: str, default: str) -> list[str]:
    raw = os.environ.get(var)
    return shlex.split(raw) if raw else [default]


def proc_stat(pid: int):
    """(state, starttime) from /proc/<pid>/stat, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    rest = data[data.rfind(")") + 2:].split()
    try:
        return rest[0], int(rest[19])
    except (IndexError, ValueError):
        return None


def proc_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _hyprlock_running() -> bool:
    try:
        names = os.listdir("/proc")
    except OSError:
        return False
    for name in names:
        if name.isdigit() and proc_comm(int(name)) == "hyprlock":
            return True
    return False


def _have_pil() -> bool:
    try:
        import PIL.Image  # noqa: F401
        return True
    except Exception:
        return False


class DaemonError(Exception):
    pass


class VisionError(Exception):
    pass


class HyprError(Exception):
    pass


class Interrupted(BaseException):
    """Raised by the signal handler while blocked in an interruptible wait."""


# --------------------------------------------------------------------------- config

class Group:
    def __init__(self, gid, label, enabled, webapp_classes, title_patterns, url_block):
        self.id = gid
        self.label = label
        self.enabled = enabled
        self.webapp_classes = webapp_classes
        self.title_patterns = title_patterns
        self.url_block = url_block


class Config:
    def __init__(self, watch: dict, regexes: dict, groups: list, source: str):
        self.watch = watch
        self.regexes = regexes
        self.groups = groups
        self.source = source

    def __getattr__(self, key):
        try:
            return self.__dict__["watch"][key]
        except KeyError:
            raise AttributeError(key) from None


def compile_patterns(patterns, where: str) -> list:
    out = []
    if not isinstance(patterns, list):
        if patterns is not None:
            log(f"config: {where} is not a list, ignored")
        return out
    for p in patterns:
        if not isinstance(p, str):
            log(f"config: {where}: non-string pattern {p!r} dropped")
            continue
        try:
            out.append(re.compile(p))
        except re.error as e:
            log(f"config: {where}: bad regex {p!r} dropped ({e})")
    return out


def build_config(data: dict, source: str = "") -> Config:
    raw = data.get("watch") if isinstance(data.get("watch"), dict) else {}
    w = dict(DEFAULT_WATCH)
    mode = raw.get("mode", w["mode"])
    if mode not in MODES:
        log(f"config: watch.mode {mode!r} unknown, using 'observe'")
        mode = "observe"
    w["mode"] = mode
    for key, (lo, hi, integer) in NUMERIC_KEYS.items():
        if key not in raw:
            continue
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)) \
                or not lo <= v <= hi:
            log(f"config: watch.{key} = {v!r} invalid, using {w[key]!r}")
            continue
        w[key] = v
    vi = raw.get("vision_interval_seconds", w["vision_interval_seconds"])
    if (isinstance(vi, list) and len(vi) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vi)
            and 10 <= vi[0] <= vi[1] <= 86400):
        w["vision_interval_seconds"] = [float(vi[0]), float(vi[1])]
    else:
        log(f"config: watch.vision_interval_seconds = {vi!r} invalid, using the default")
        w["vision_interval_seconds"] = list(DEFAULT_WATCH["vision_interval_seconds"])
    for key in ("ollama_url", "model"):
        v = raw.get(key, w[key])
        if isinstance(v, str) and v.strip():
            w[key] = v.strip()
        else:
            log(f"config: watch.{key} invalid, using {w[key]!r}")
    regexes = {}
    for key in REGEX_KEYS:
        regexes[key] = compile_patterns(raw.get(key, w[key]), f"watch.{key}")
    groups = []
    raw_groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    for gid, g in raw_groups.items():
        if not GROUP_ID_RE.match(gid) or not isinstance(g, dict):
            log(f"config: group {gid!r} invalid, dropped")
            continue
        label = g.get("label") if isinstance(g.get("label"), str) and g.get("label") else gid
        url_block = [u for u in g.get("url_block", []) if isinstance(u, str)] \
            if isinstance(g.get("url_block"), list) else []
        groups.append(Group(
            gid, label, g.get("enabled", True) is not False,
            compile_patterns(g.get("webapp_classes", []), f"groups.{gid}.webapp_classes"),
            compile_patterns(g.get("title_patterns", []), f"groups.{gid}.title_patterns"),
            url_block,
        ))
    return Config(w, regexes, groups, source)


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return build_config(data, path)


# --------------------------------------------------------------------------- runtime pieces

def default_paths() -> dict:
    root = os.environ.get("FOCUS_ROOT", "")
    state_home = os.environ.get("XDG_STATE_HOME", "")
    if not os.path.isabs(state_home):
        state_home = os.path.join(os.path.expanduser("~"), ".local", "state")
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not (os.path.isabs(runtime) and os.path.isdir(runtime)):
        candidate = f"/run/user/{os.getuid()}"
        runtime = candidate if os.path.isdir(candidate) else ""
    return {
        "config": os.environ.get("FOCUS_CONFIG") or root + "/etc/focus/config.toml",
        "socket": os.environ.get("FOCUS_SOCKET") or root + "/run/focusd/focusd.sock",
        "state_dir": os.path.join(state_home, "focus"),
        "runtime": runtime,
        "power": os.environ.get("FOCUS_POWER_SUPPLY") or "/sys/class/power_supply",
    }


def daemon_request(path: str, obj: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(obj).encode() + b"\n"
    buf = b""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(data)
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 8 * 1024 * 1024:
                    raise DaemonError("response too large")
    except OSError as e:
        raise DaemonError(f"{type(e).__name__}: {e}") from None
    try:
        resp = json.loads(buf)
    except ValueError:
        raise DaemonError("unparseable response") from None
    if not isinstance(resp, dict):
        raise DaemonError("response is not an object")
    return resp


class Hypr:
    def __init__(self, cmd: list[str]):
        self.cmd = cmd

    def run(self, args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self.cmd + args, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise HyprError(f"{' '.join(args[:1])}: {e}") from None

    def json(self, what: str):
        r = self.run([what, "-j"])
        if r.returncode != 0:
            raise HyprError(f"hyprctl {what}: exit {r.returncode}: {r.stdout.strip()[:200]}")
        text = r.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise HyprError(f"hyprctl {what}: not JSON") from None

    def active_window(self):
        d = self.json("activewindow")
        if not isinstance(d, dict) or not d.get("address"):
            return None
        return d

    def clients(self) -> list:
        d = self.json("clients")
        return d if isinstance(d, list) else []

    def client(self, address: str):
        for c in self.clients():
            if isinstance(c, dict) and c.get("address") == address:
                return c
        return None

    def session_locked(self) -> bool:
        """Omarchy's Quickshell lock: an ext-session-lock shows up as LOCK in solitaryBlockedBy."""
        try:
            mons = self.json("monitors")
        except HyprError:
            return False
        if not isinstance(mons, list):
            return False
        return any(isinstance(m, dict) and "LOCK" in (m.get("solitaryBlockedBy") or []) for m in mons)

    def dispatch(self, expr: str) -> tuple[bool, str]:
        try:
            r = self.run(["dispatch", expr])
        except HyprError as e:
            return False, str(e)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0 and not out.lower().startswith("error"), out


def battery_low(power_dir: str, min_percent: float):
    """(low, percent) — low only while discharging below min_percent."""
    try:
        names = sorted(n for n in os.listdir(power_dir) if n.startswith("BAT"))
    except OSError:
        return False, None
    for n in names:
        try:
            with open(os.path.join(power_dir, n, "status")) as f:
                status = f.read().strip()
            with open(os.path.join(power_dir, n, "capacity")) as f:
                capacity = float(f.read().strip())
        except (OSError, ValueError):
            continue
        if status == "Discharging" and capacity < min_percent:
            return True, capacity
    return False, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects are refused", headers, fp)


# Screenshots go to ollama_url only: no proxies from the environment, no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


# --------------------------------------------------------------------------- state

class State:
    def __init__(self, path: str):
        self.path = path
        self.streak = 0
        self.notified = False            # a notification was sent during the current streak
        self.next_vision_at = 0.0
        self.hidden: list[dict] = []
        self.dup_notified: list[str] = []  # penalty ids already re-announced (no duplicate penalties)
        self.penalized: list[dict] = []    # {id, class, stem, until} of penalties this monitor created

    def load(self) -> None:
        try:
            with open(self.path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log(f"state: {self.path} unreadable ({e}); starting fresh")
            return
        if not isinstance(d, dict):
            return
        s = d.get("streak")
        self.streak = s if isinstance(s, int) and s >= 0 else 0
        self.notified = d.get("notified") is True
        dn = d.get("dup_notified")
        self.dup_notified = [x for x in dn if isinstance(x, str)] if isinstance(dn, list) else []
        pz = d.get("penalized")
        self.penalized = [x for x in pz if isinstance(x, dict) and x.get("id")] if isinstance(pz, list) else []
        nv = d.get("next_vision_at")
        self.next_vision_at = float(nv) if isinstance(nv, (int, float)) and not isinstance(nv, bool) else 0.0
        hidden = d.get("hidden")
        self.hidden = [h for h in hidden if isinstance(h, dict)] if isinstance(hidden, list) else []

    def save(self) -> None:
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        data = json.dumps({"version": 1, "streak": self.streak, "notified": self.notified,
                           "next_vision_at": self.next_vision_at, "hidden": self.hidden,
                           "dup_notified": self.dup_notified[-50:], "penalized": self.penalized[-50:]},
                          indent=1).encode()
        fd, tmp = tempfile.mkstemp(prefix=".watch.", suffix=".tmp", dir=d)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, self.path)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)


# --------------------------------------------------------------------------- the monitor

class Watcher:
    def __init__(self, dry_run: bool = False, no_report: bool = False, once: bool = False):
        self.dry_run = dry_run
        self.no_report = no_report
        self.once = once
        self.paths = default_paths()
        self.hypr = Hypr(command_from_env("FOCUS_HYPRCTL", "hyprctl"))
        self.grim_cmd = command_from_env("FOCUS_GRIM", "grim")
        self.notify_cmd = command_from_env("FOCUS_NOTIFY", "notify-send")
        self.state = State(os.path.join(self.paths["state_dir"], "watch.json"))
        self.cfg: Config | None = None
        self._cfg_sig = None
        self._stopping = False   # set by the signal handler, nothing else happens there
        self._cache = None       # (time, status) of the last successful status
        self._last_logged = None
        self.actions: list[dict] = []

    # ---- config

    def _sig(self):
        try:
            st = os.stat(self.paths["config"])
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def initial_config(self) -> bool:
        """Load the config at start. False if the monitor must refuse to start."""
        self._cfg_sig = self._sig()
        try:
            self.cfg = load_config(self.paths["config"])
        except (OSError, tomllib.TOMLDecodeError) as e:
            log(f"config: cannot load {self.paths['config']} ({e}); using defaults")
            self.cfg = build_config({}, "defaults")
        if not is_loopback_url(self.cfg.ollama_url):
            log(f"refusing to start: watch.ollama_url {self.cfg.ollama_url!r} is not a loopback address")
            return False
        if is_cloud_model(self.cfg.model):
            log(f"refusing to start: watch.model {self.cfg.model!r} is a cloud model (it would upload screenshots)")
            return False
        return True

    def maybe_reload(self) -> None:
        sig = self._sig()
        if sig == self._cfg_sig:
            return
        self._cfg_sig = sig
        if sig is None:
            log("config: file disappeared; keeping the previous config")
            return
        try:
            new = load_config(self.paths["config"])
        except (OSError, tomllib.TOMLDecodeError) as e:
            log(f"config: reload failed ({e}); keeping the previous config")
            return
        if not is_loopback_url(new.ollama_url):
            log(f"config: watch.ollama_url {new.ollama_url!r} is not loopback; keeping {self.cfg.ollama_url!r}")
            new.watch["ollama_url"] = self.cfg.ollama_url
        if is_cloud_model(new.model):
            log(f"config: watch.model {new.model!r} is a cloud model; keeping {self.cfg.model!r}")
            new.watch["model"] = self.cfg.model
        self.cfg = new
        log(f"config: reloaded (mode {new.mode})")

    # ---- side effects (all funnel through here so --dry-run is honoured)

    def daemon(self, obj: dict, timeout: float = 10.0) -> dict:
        return daemon_request(self.paths["socket"], obj, timeout)

    def notify(self, urgency: str, body: str) -> None:
        body = html.escape(body, quote=False)  # notification daemons render Pango markup
        self.actions.append({"action": "notify", "urgency": urgency, "body": body, "dry_run": self.dry_run})
        if self.dry_run:
            return
        try:
            subprocess.run(self.notify_cmd + ["-u", urgency, "Focus", body], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"notify failed: {e}")

    def save_state(self) -> None:
        if self.dry_run:
            return
        try:
            self.state.save()
        except OSError as e:
            log(f"state: cannot save {self.state.path}: {e}")

    # ---- restore

    @staticmethod
    def _pid_gone(e: dict) -> bool:
        pid = e.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            return False  # unknown: can't confirm anything from the pid
        st = proc_stat(pid)
        return st is None or (e.get("pid_start") is not None and st[1] != e.get("pid_start"))

    def _resume(self, e: dict, act: dict) -> None:
        pid = e.get("pid")
        if not e.get("frozen") or not isinstance(pid, int) or pid <= 1:
            return
        if self._pid_gone(e):  # dead, or the pid now belongs to another process
            act["continued"] = False
            e["frozen"] = False
            return
        try:
            os.kill(pid, signal.SIGCONT)
            act["continued"] = True
            e["frozen"] = False
        except OSError as err:
            act["continued"] = False
            log(f"restore: SIGCONT {pid} failed: {err}")

    def _restore(self, entries: list[dict], why: str) -> None:
        """Resume and un-hide `entries`. An entry is dropped only once its window is back (or no longer
        in the jail) or confirmed gone; anything else stays recorded and is retried on the next tick."""
        if not entries:
            return
        acts = []
        for e in entries:
            act = {"action": "restore", "penalty_id": e.get("penalty_id"), "address": e.get("address"),
                   "class": e.get("class"), "frozen": bool(e.get("frozen")), "why": why,
                   "dry_run": self.dry_run}
            self.actions.append(act)
            acts.append(act)
        if self.dry_run:
            return
        done = set()
        # Resume every process first, so nothing stays frozen if hyprctl misbehaves.
        for e, act in zip(entries, acts):
            self._resume(e, act)
            if self._pid_gone(e):
                act["gone"] = "process"
                done.add(id(e))

        def jailed_clients():
            return {c.get("address"): c for c in self.hypr.clients() if isinstance(c, dict)}
        try:
            clients = jailed_clients()
        except HyprError as err:
            clients = None
            log(f"restore: cannot list windows ({err}); will retry")
        moved = []
        for e, act in zip(entries, acts):
            if id(e) in done or clients is None:
                continue
            address = e.get("address")
            c = clients.get(address) if isinstance(address, str) and ADDRESS_RE.match(address) else None
            # Hyprland reuses addresses: a different class there means our window is gone.
            if c is None or c.get("class") != e.get("class"):
                act["gone"] = "window"
                done.add(id(e))
                continue
            if (c.get("workspace") or {}).get("name") != JAIL:
                act["moved"] = "already"
                done.add(id(e))
                continue
            target = self._restore_target(e)
            ok, out = self.hypr.dispatch(move_expr(address, target))
            if ok:
                moved.append((e, act))
            else:
                act["moved"] = False
                log(f"restore: moving {address} back to {target} failed: {out}")
        if moved:
            try:
                after = jailed_clients()
            except HyprError:
                after = None  # the dispatch said ok for a window that existed a moment ago: accept it
            for e, act in moved:
                c = after.get(e.get("address")) if after is not None else None
                if c is not None and (c.get("workspace") or {}).get("name") == JAIL:
                    act["moved"] = False
                else:
                    act["moved"] = True
                    done.add(id(e))
        for e, act in zip(entries, acts):
            act["kept"] = id(e) not in done
        self.state.hidden = [h for h in self.state.hidden if id(h) not in done]
        self.save_state()

    def _restore_target(self, e: dict) -> str:
        name, wid = e.get("workspace"), e.get("workspace_id")
        if name == JAIL or not (name or (isinstance(wid, int) and wid >= 1)):
            try:
                aw = self.hypr.json("activeworkspace")
                if isinstance(aw, dict) and isinstance(aw.get("id"), int) and aw["id"] >= 1:
                    return str(aw["id"])
            except HyprError:
                pass
            return "1"
        return workspace_selector(wid, name)

    @staticmethod
    def _expired(e: dict, now: float) -> bool:
        until = e.get("until")
        return isinstance(until, (int, float)) and not isinstance(until, bool) and until <= now

    def restore_ended(self, status: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        active = {p.get("id") for p in (status.get("penalties") or []) if isinstance(p, dict)}
        ended = [e for e in self.state.hidden if e.get("penalty_id") not in active or self._expired(e, now)]
        self._restore(ended, "penalty_ended")

    def restore_expired(self, now: float | None = None) -> None:
        """Daemon unreachable: penalties can only end early, so a passed `until` is safe to act on."""
        now = time.time() if now is None else now
        self._restore([e for e in self.state.hidden if self._expired(e, now)], "penalty_expired")

    def restore_all(self, why: str) -> None:
        self._restore(list(self.state.hidden), why)

    def cleanup_temp(self) -> None:
        rd = self.paths["runtime"]
        if not rd:
            return
        try:
            names = os.listdir(rd)
        except OSError:
            return
        cutoff = time.time() - STALE_TEMP_SECONDS
        for n in names:
            if n.startswith("focus-watch-") and n.endswith((".ppm", ".jpg")):
                path = os.path.join(rd, n)
                try:
                    if os.lstat(path).st_mtime < cutoff:
                        os.unlink(path)
                except OSError:
                    pass

    # ---- classification

    def _groups(self, status):
        """[(Group, unlocked)] for enabled groups. Patterns come from the config, lock state from focusd.
        Without a (recent) status the lock state is unknown, so no group matches at all."""
        if not status:
            return []
        groups = list(self.cfg.groups)
        known = {g.id for g in groups}
        unlocked = set()
        disabled = set()
        for sg in status.get("groups") or []:
            if not isinstance(sg, dict) or not isinstance(sg.get("id"), str):
                continue
            if sg.get("enabled", True) is False:
                disabled.add(sg["id"])
            elif sg.get("locked") is False:
                unlocked.add(sg["id"])
            if sg["id"] not in known and GROUP_ID_RE.match(sg["id"]):
                groups.append(Group(
                    sg["id"], str(sg.get("label") or sg["id"]), sg.get("enabled", True) is not False,
                    compile_patterns(sg.get("webapp_classes") or [], f"status {sg['id']} webapp_classes"),
                    compile_patterns(sg.get("title_patterns") or [], f"status {sg['id']} title_patterns"),
                    [u for u in sg.get("url_block") or [] if isinstance(u, str)],
                ))
        return [(g, g.id in unlocked) for g in groups if g.enabled and g.id not in disabled]

    @staticmethod
    def _match_text(m: re.Match, pattern: re.Pattern) -> str:
        text = m.group(0).strip(" \t-–—|:·")
        return text or pattern.pattern

    def classify_window(self, win, status) -> dict:
        cls, title = str(win.get("class") or ""), str(win.get("title") or "")
        base = {"class": cls, "title": title, "domain": "", "confidence": 1.0}
        rx = self.cfg.regexes
        # Class exemptions first: terminals, editors, work apps are never judged by their title.
        for key in ("never_touch_classes", "work_classes"):
            if any(p.search(cls) for p in rx[key]):
                return {**base, "verdict": "working", "source": "title", "app": cls,
                        "description": f"window class {cls} is work"}
        # Titles are only trusted for browsers and web apps; native apps get class rules and vision.
        web = bool(WEBAPP_RE.match(cls)) or any(p.search(cls) for p in rx["browser_classes"])
        groups = self._groups(status)
        for g, unlocked in groups:
            if not unlocked:
                continue
            if any(p.search(cls) for p in g.webapp_classes) or (web and any(p.search(title) for p in g.title_patterns)):
                return {**base, "verdict": "allowed", "source": "unlock", "app": g.label,
                        "description": f"{g.label} is unlocked"}
        for g, unlocked in groups:
            if unlocked:
                continue
            if web and any(p.search(title) for p in g.title_patterns):
                return {**base, "verdict": "procrastinating", "source": "title", "app": g.label,
                        "description": f"window title matches {g.label}"}
            if any(p.search(cls) for p in g.webapp_classes):
                return {**base, "verdict": "procrastinating", "source": "title", "app": g.label,
                        "description": f"{g.label} web app"}
        if web:
            for p in rx["distraction_title_patterns"]:
                m = p.search(title)
                if m:
                    app = self._match_text(m, p)
                    return {**base, "verdict": "procrastinating", "source": "title", "app": app,
                            "description": f"window title matches distraction pattern {p.pattern!r}"}
            for p in rx["work_title_patterns"]:
                if p.search(title):
                    return {**base, "verdict": "working", "source": "title", "app": cls,
                            "description": f"window title matches work pattern {p.pattern!r}"}
        return {**base, "verdict": "undecided", "source": "title", "app": cls, "confidence": 0.0,
                "description": ""}

    def vision_step(self, obs: dict, win: dict, status, now: float) -> dict:
        cfg = self.cfg
        if not self.once and now < self.state.next_vision_at:
            obs["description"] = f"title undecided; next screenshot in {int(self.state.next_vision_at - now)} s"
            return obs
        low, pct = battery_low(self.paths["power"], cfg.vision_min_battery_percent)
        if low:
            obs.update(source="vision", description=f"screenshot skipped: on battery at {pct:.0f}%")
            return obs
        t0 = time.monotonic()
        try:
            result = self.vision(win)
            obs.update(verdict=result["verdict"], source="vision", app=result["app"],
                       description=result["description"], domain=result["domain"],
                       confidence=result["confidence"])
        except Exception as e:  # noqa: BLE001 - any vision failure is an `error` verdict
            obs.update(verdict="error", source="vision", confidence=0.0,
                       description=trunc(f"vision failed: {type(e).__name__}: {e}"))
        obs["vision_seconds"] = round(time.monotonic() - t0, 1)
        self.state.next_vision_at = time.time() + random.uniform(*cfg.vision_interval_seconds)
        return obs

    def capture(self, win: dict) -> bytes:
        """Screenshot of the window rectangle → downscaled JPEG bytes. Temp files never survive."""
        try:
            x, y = (int(v) for v in win["at"])
            w, h = (int(v) for v in win["size"])
        except (KeyError, TypeError, ValueError):
            raise VisionError("active window has no geometry") from None
        if w <= 0 or h <= 0:
            raise VisionError(f"window size {w}x{h}")
        rd = self.paths["runtime"]
        if not rd:
            raise VisionError("no XDG_RUNTIME_DIR: refusing to write a screenshot elsewhere")
        raw = small = None
        try:
            fd, raw = tempfile.mkstemp(prefix="focus-watch-", suffix=".ppm", dir=rd)  # 0600
            os.close(fd)
            r = subprocess.run(self.grim_cmd + ["-t", "ppm", "-g", f"{x},{y} {w}x{h}", raw],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
            if r.returncode != 0:
                raise VisionError(f"grim exit {r.returncode}: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
            if os.path.getsize(raw) == 0:
                raise VisionError("grim wrote nothing")
            max_w = int(self.cfg.image_max_width)
            if _have_pil():
                from PIL import Image
                with Image.open(raw) as im:
                    im = im.convert("RGB")
                    if im.width > max_w:
                        im = im.resize((max_w, max(1, round(im.height * max_w / im.width))),
                                       Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=85)
                    return buf.getvalue()
            fd, small = tempfile.mkstemp(prefix="focus-watch-", suffix=".jpg", dir=rd)
            os.close(fd)
            r = subprocess.run(["magick", raw, "-resize", f"{max_w}x>", "-quality", "85", "jpeg:" + small],
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            if r.returncode != 0:
                raise VisionError(f"magick exit {r.returncode}")
            with open(small, "rb") as f:
                return f.read()
        finally:
            for p in (raw, small):
                if p:
                    try:
                        os.unlink(p)
                    except FileNotFoundError:
                        pass

    def _post(self, req, timeout: float) -> bytes:
        """POST in a worker thread, so a stop request (the signal handler only sets a flag) is noticed
        within 0.2 s instead of after the model answers."""
        if self._stopping:
            raise Interrupted()
        box = {}

        def work():
            try:
                with _OPENER.open(req, timeout=timeout) as r:
                    box["data"] = r.read(4 * 1024 * 1024)
            except urllib.error.HTTPError as e:
                e.close()
                box["error"] = VisionError(f"HTTP {e.code}: {e.reason}")
            except BaseException as e:  # noqa: BLE001 - handed to the caller
                box["error"] = e
        t = threading.Thread(target=work, name="focus-watch-ollama", daemon=True)
        t.start()
        while t.is_alive():
            if self._stopping:
                raise Interrupted()
            t.join(0.2)
        if "error" in box:
            raise box["error"]
        return box["data"]

    def vision(self, win: dict) -> dict:
        cfg = self.cfg
        if not is_loopback_url(cfg.ollama_url):  # belt and braces; checked at start and on reload
            raise VisionError("ollama_url is not loopback")
        jpeg = self.capture(win)
        # Measured: listing Theo's blocked groups here biased the model to call everything else
        # "working" (a game, Hacker News), so only the class and title are given.
        text = (f"Window class: {trunc(win.get('class'), 200)}\n"
                f"Window title: {trunc(win.get('title'), 300)}\n"
                "What is in this window? Reply with the JSON verdict.")
        body = {
            "model": cfg.model,
            "stream": False,
            "think": False,
            "format": VISION_SCHEMA,
            "options": {"temperature": 0, "num_ctx": int(cfg.num_ctx)},
            "keep_alive": "10m",
            "messages": [
                {"role": "system", "content": VISION_SYSTEM},
                {"role": "user", "content": text, "images": [base64.b64encode(jpeg).decode()]},
            ],
        }
        del jpeg
        req = urllib.request.Request(cfg.ollama_url.rstrip("/") + "/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        del body
        resp = json.loads(self._post(req, float(cfg.vision_timeout_seconds)))
        try:
            content = json.loads(resp["message"]["content"])
        except (KeyError, TypeError, ValueError):
            raise VisionError("model reply is not the JSON verdict") from None
        if not isinstance(content, dict):
            raise VisionError("model reply is not an object")
        verdict = content.get("verdict")
        if verdict not in ("working", "procrastinating", "unclear"):
            verdict = "unclear"
        try:
            conf = float(content.get("confidence"))
            conf = 0.0 if conf != conf else min(1.0, max(0.0, conf))
        except (TypeError, ValueError):
            conf = 0.0
        domain = normalize_domain(content.get("domain"))
        app = str(content.get("app") or "").strip()
        # The model likes to name the browser (or echo the class); the site is what matters to Theo.
        cls = str(win.get("class") or "")
        if domain and (not app or app.lower() == cls.lower()
                       or any(p.search(app) or p.search(app.lower()) for p in self.cfg.regexes["browser_classes"])):
            app = domain
        return {"verdict": verdict, "confidence": conf, "app": trunc(app or cls),
                "description": trunc(content.get("description")), "domain": domain}

    # ---- enforcement

    def _class_rule(self, cls: str) -> str:
        rx = self.cfg.regexes
        for rule, key in (("never_touch", "never_touch_classes"), ("browser", "browser_classes"),
                          ("freeze", "freeze_classes")):
            if any(p.search(cls) for p in rx[key]):
                return rule
        return "hide"

    def _can_freeze(self, pid, cls: str) -> bool:
        if not isinstance(pid, int) or pid <= 1 or pid in (os.getpid(), os.getppid()):
            return False
        rx = self.cfg.regexes
        if any(p.search(cls) for p in rx["browser_classes"]) or any(p.search(cls) for p in rx["never_touch_classes"]):
            return False
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                return False
        except OSError:
            return False
        comm = proc_comm(pid)
        return bool(comm) and not NEVER_FREEZE_COMM.match(comm)

    def identify_domain(self, win: dict) -> str:
        """One immediate vision call (ignoring next_vision_at) to find the site behind a browser title hit."""
        act = {"action": "identify_domain"}
        self.actions.append(act)
        low, pct = battery_low(self.paths["power"], self.cfg.vision_min_battery_percent)
        if low:
            act["skipped"] = f"battery {pct:.0f}%"
            return ""
        try:
            act["domain"] = self.vision(win)["domain"]
        except Exception as e:  # noqa: BLE001 - no domain then
            act["error"] = trunc(f"{type(e).__name__}: {e}")
            return ""
        return act["domain"]

    def _duplicate(self, status, domain, app: str, cls: str, title: str):
        """An active penalty for the same thing: same domain; or the same app where either side has no
        domain (focusd drops domains it can't confirm in the DNS log); or one this monitor created for
        the same window class and title stem."""
        pens = [p for p in (status or {}).get("penalties") or [] if isinstance(p, dict)]
        active = {p.get("id") for p in pens}
        self.state.penalized = [x for x in self.state.penalized if x.get("id") in active]
        mine = {x["id"]: x for x in self.state.penalized}
        app_k, stem = str(app or "").strip().casefold(), title_stem(title)
        for p in pens:
            p_domain = normalize_domain(p.get("domain") or "")
            if domain and p_domain == domain:
                return p
            if app_k and (not domain or not p_domain) and str(p.get("app") or "").strip().casefold() == app_k:
                return p
            x = mine.get(p.get("id"))
            if stem and x and x.get("class") == cls and x.get("stem") == stem:
                return p
        return None

    def _on_duplicate(self, dup: dict, obs: dict, win: dict, app: str, cls: str, status: dict) -> None:
        self.actions.append({"action": "duplicate", "penalty_id": dup.get("id")})
        # A window that isn't hidden yet (e.g. a web app reopened under a new address) goes to the jail
        # under the existing penalty and its `until`.
        if self._class_rule(cls) in ("hide", "freeze") and dup.get("id") \
                and not any(h.get("address") == win.get("address") for h in self.state.hidden):
            self.hide(dup, win, obs, freeze=(self._class_rule(cls) == "freeze"))
        active = {p.get("id") for p in (status.get("penalties") or []) if isinstance(p, dict)}
        self.state.dup_notified = [x for x in self.state.dup_notified if x in active]
        if dup.get("id") not in self.state.dup_notified:
            try:
                left = f" ({max(1, round((float(dup['until']) - time.time()) / 60))} min left)"
            except (KeyError, TypeError, ValueError):
                left = ""
            self.notify("normal", f"{app} is already blocked{left}. Back to work?")
            self.state.dup_notified.append(str(dup.get("id")))

    def enforce(self, obs: dict, win: dict, streak: int, status: dict) -> None:
        cfg = self.cfg
        app = obs.get("app") or obs.get("class") or "this"
        cls = obs.get("class") or ""
        title = obs.get("title") or ""
        domain = (obs.get("domain") or None) if obs.get("source") == "vision" else None
        # Dedupe first, so a repeat costs no screenshot.
        dup = self._duplicate(status, domain, app, cls, title)
        if dup is None and domain is None and obs.get("source") == "title" \
                and any(p.search(cls) for p in cfg.regexes["browser_classes"]):
            domain = self.identify_domain(win) or None
            if domain:
                dup = self._duplicate(status, domain, app, cls, title)
        if dup is not None:
            self._on_duplicate(dup, obs, win, app, cls, status)
            return
        req = {"op": "penalize", "app": trunc(app), "title": trunc(title),
               "description": trunc(obs.get("description")), "source": obs.get("source"), "domain": domain,
               "reason": trunc(f"{streak} consecutive procrastinating checks; last one by {obs.get('source')} "
                               f"with confidence {float(obs.get('confidence') or 0):.2f}")}
        if self.dry_run:
            self.actions.append({"action": "penalize", "request": req, "dry_run": True})
            penalty = {"id": None, "since": time.time(), "until": time.time() + cfg.penalty_minutes * 60}
        else:
            error = None
            try:
                resp = self.daemon(req, timeout=30)
                penalty = resp.get("penalty") if resp.get("ok") else None
                if not isinstance(penalty, dict) or not penalty.get("id"):
                    error = resp.get("error") or "bad_response"
            except DaemonError as e:
                error = "unreachable"
                log(f"penalize: daemon unreachable ({e})")
            self.actions.append({"action": "penalize", "request": req, "error": error,
                                 "penalty_id": None if error else penalty["id"]})
            if error:
                self.notify("normal", f"Looks like {app}. Back to work? (no penalty: {error})")
                return
            self.state.penalized.append({"id": penalty["id"], "class": cls, "stem": title_stem(title),
                                         "until": penalty.get("until")})
        rule = self._class_rule(cls)
        if rule == "hide" or rule == "freeze":
            self.hide(penalty, win, obs, freeze=(rule == "freeze"))
        else:
            self.actions.append({"action": "none", "rule": rule, "address": win.get("address"), "class": cls})
        try:
            minutes = max(1, round((float(penalty["until"]) - float(penalty["since"])) / 60))
        except (KeyError, TypeError, ValueError):
            minutes = cfg.penalty_minutes
        self.notify("critical", f"{app} is blocked for {minutes} min. Appeal: focus appeal")

    def hide(self, penalty: dict, win: dict, obs: dict, freeze: bool) -> None:
        address, cls, pid = win.get("address"), str(win.get("class") or ""), win.get("pid")
        ws = win.get("workspace") or {}
        act = {"action": "hide", "address": address, "class": cls, "freeze": freeze, "dry_run": self.dry_run}
        self.actions.append(act)
        if not isinstance(address, str) or not ADDRESS_RE.match(address):
            act["error"] = "bad address"
            return
        can_freeze = freeze and self._can_freeze(pid, cls)
        act["freeze"] = can_freeze
        if self.dry_run:
            return
        if any(h.get("address") == address for h in self.state.hidden):
            act["error"] = "already hidden"
            return
        try:
            current = self.hypr.client(address)
        except HyprError as e:
            act["error"] = str(e)
            return
        # The window may have closed (and its address been reused) since it was classified.
        if current is None or current.get("class") != win.get("class") or current.get("pid") != pid:
            act["error"] = "window gone"
            return
        ws = current.get("workspace") or ws
        if ws.get("name") == JAIL:  # already hidden (e.g. Theo opened the jail): never record the jail
            act["error"] = "already in the jail"
            return
        st = proc_stat(pid) if isinstance(pid, int) and pid > 1 else None
        until = penalty.get("until")
        if not isinstance(until, (int, float)) or isinstance(until, bool):
            until = time.time() + self.cfg.penalty_minutes * 60
        entry = {"penalty_id": penalty["id"], "address": address, "class": cls,
                 "title": trunc(win.get("title")), "workspace": ws.get("name"), "workspace_id": ws.get("id"),
                 "pid": pid, "pid_start": st[1] if st else None, "frozen": False,
                 "since": time.time(), "until": until}
        # Recorded before the move, so a crash right after it can never leave an unrecorded hidden window.
        self.state.hidden.append(entry)
        self.save_state()
        ok, out = self.hypr.dispatch(move_expr(address, JAIL))
        try:
            # hyprctl says "ok" even for an address that matches nothing, so check where it went.
            current = self.hypr.client(address)
            in_jail = ((current or {}).get("workspace") or {}).get("name") == JAIL
        except HyprError:
            in_jail = None  # unknown: keep the entry; restore checks the jail before moving anything
        if in_jail is not True:
            act["error"] = f"move {'failed' if not ok else 'unverified'}: {out}"
            log(f"hide: {act['error']}")
            if in_jail is False:
                self.state.hidden = [h for h in self.state.hidden if h is not entry]
                self.save_state()
            return
        act["hidden"] = True
        if can_freeze and st:
            # Persist before SIGSTOP so a crash can never leave an unrecorded frozen process.
            entry["frozen"] = True
            self.save_state()
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError as e:
                log(f"freeze: SIGSTOP {pid} failed: {e}")
                entry["frozen"] = False
                self.save_state()
        act["freeze"] = entry["frozen"]

    # ---- one tick

    def tick(self) -> dict:
        self.actions = []
        self.maybe_reload()
        self.cleanup_temp()
        cfg = self.cfg
        now = time.time()
        live = None
        try:
            resp = self.daemon({"op": "status"})
            if resp.get("ok"):
                live = resp
            else:
                log(f"status refused: {resp.get('error')}")
        except DaemonError as e:
            log(f"daemon unreachable: {e}")
        if live is not None:
            self._cache = (now, live)
        # Unreachable: the last good status still says what's unlocked, for a while; then it's unknown.
        view = live
        if view is None and self._cache and now - self._cache[0] <= STATUS_CACHE_SECONDS:
            view = self._cache[1]
        mode = cfg.mode
        if view and isinstance(view.get("watch"), dict) and view["watch"].get("mode") in MODES:
            mode = view["watch"]["mode"]
        result = {"t": round(now, 3), "mode": mode, "daemon": "ok" if live else "unreachable",
                  "status_cached": live is None and view is not None, "dry_run": self.dry_run}
        if mode == "off":
            self.restore_all("mode_off")
            result.update(verdict=None, actions=self.actions)
            return result
        if live is not None:
            self.restore_ended(live, now)
        else:
            self.restore_expired(now)

        win = None
        try:
            if _hyprlock_running() or self.hypr.session_locked():
                obs = {"verdict": "idle", "source": "idle", "app": "", "class": "", "title": "",
                       "description": "session locked", "domain": "", "confidence": 1.0}
            else:
                win = self.hypr.active_window()
                if win is None:
                    obs = {"verdict": "idle", "source": "idle", "app": "", "class": "", "title": "",
                           "description": "no active window", "domain": "", "confidence": 1.0}
                else:
                    obs = self.classify_window(win, view)
                    if obs["verdict"] == "undecided":
                        obs = self.vision_step(obs, win, view, now)
        except HyprError as e:
            obs = {"verdict": "error", "source": "title", "app": "", "class": "", "title": "",
                   "description": trunc(f"hyprctl failed: {e}"), "domain": "", "confidence": 0.0}

        warned_before = self.state.notified
        streak = self.state.streak
        counted = obs["verdict"] == "procrastinating" and float(obs.get("confidence") or 0) >= cfg.min_confidence
        if counted:
            streak += 1
        elif obs["verdict"] in ("working", "allowed"):
            streak = 0
        if streak == 0:
            self.state.notified = False
        obs["streak"] = streak

        if not self.no_report:
            report = {"op": "observe", **{k: obs.get(k) for k in (
                "verdict", "source", "app", "class", "title", "description", "domain", "confidence", "streak")}}
            for k in ("app", "class", "title", "description", "domain"):
                report[k] = trunc(report[k])
            try:
                self.daemon(report)
            except DaemonError as e:
                if live:
                    log(f"observe failed: {e}")

        if counted and mode in ("nudge", "enforce"):  # only on a tick that extended the streak
            if streak % cfg.notify_after == 0:
                if self.once and not self.dry_run:
                    self.actions.append({"action": "notify", "skipped": "--once"})
                else:
                    self.notify("normal", f"Looks like {obs.get('app') or obs.get('class')}. Back to work?")
                    self.state.notified = True
            if mode == "enforce" and streak >= cfg.enforce_after:
                why = None
                if self.once:
                    why = "--once never enforces"
                elif not warned_before:
                    why = "no warning yet in this streak"
                elif live is None:
                    why = "daemon unreachable"
                elif self._stopping:
                    why = "shutting down"
                if why:
                    self.actions.append({"action": "skip_enforce", "why": why})
                else:
                    self.enforce(obs, win or {}, streak, live)
                    streak = 0
                    self.state.notified = False
        self.state.streak = streak
        self.save_state()

        key = (obs["verdict"], obs.get("app"), obs.get("title"))
        if key != self._last_logged and not self.dry_run:
            log(f"{obs['verdict']} ({obs['source']}) {obs.get('app')!s:.60} | {obs.get('title')!s:.80} streak={streak}")
            self._last_logged = key
        result.update(obs)
        result["streak"] = streak
        result["actions"] = self.actions
        return result

    # ---- loop

    def _on_signal(self, signum, frame):
        self._stopping = True  # nothing else: no locks, no I/O, no exceptions in a signal handler

    def install_signals(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while not self._stopping:
            left = end - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.25, left))

    def run(self) -> int:
        self.install_signals()
        log(f"started (version {VERSION}, mode {self.cfg.mode}{', dry run' if self.dry_run else ''})")
        try:
            while not self._stopping:
                t0 = time.monotonic()
                try:
                    result = self.tick()
                    if self.dry_run:
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                except Interrupted:
                    break
                except Exception:  # noqa: BLE001 - the monitor must keep running
                    log("tick failed:\n" + traceback.format_exc())
                interval = float(self.cfg.title_interval_seconds) * random.uniform(0.9, 1.1)
                self.sleep(interval - (time.monotonic() - t0))
        finally:
            # Never leave apps frozen or hidden behind a stopped monitor.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if not self.dry_run:
                self.actions = []
                try:
                    self.restore_all("shutdown")
                except Exception:  # noqa: BLE001
                    log("restore on shutdown failed:\n" + traceback.format_exc())
                if self.state.hidden:
                    log(f"{len(self.state.hidden)} window(s) could not be restored yet (focus-watch --restore retries)")
            log("stopped")
        return 0


def _acquire_lock(state_dir: str):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(state_dir, "watch.lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="focus-watch", description="focus screen monitor (runs as the desktop user)")
    ap.add_argument("--once", action="store_true", help="run one check, print the result as JSON, exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the actions it would take; no window moves, signals, notifications or penalties")
    ap.add_argument("--no-report", action="store_true", help="don't send `observe` to focusd")
    ap.add_argument("--restore", action="store_true",
                    help="un-freeze and un-hide every window focus-watch recorded, then exit (used by ExecStopPost)")
    ap.add_argument("--version", action="version", version=f"focus-watch {VERSION}")
    args = ap.parse_args(argv)

    w = Watcher(dry_run=args.dry_run, no_report=args.no_report, once=args.once)
    if not w.initial_config() and not args.restore:  # restoring needs neither Ollama nor the model
        return 1
    lock_fd = None
    if not args.dry_run:
        lock_fd = _acquire_lock(w.paths["state_dir"])
        if lock_fd is None:
            log("another focus-watch is running (use --dry-run to check alongside it)")
            return 1
    w.state.load()
    w.cleanup_temp()
    try:
        if args.restore:
            w.restore_all("restore")
            kept = [a for a in w.actions if a.get("kept")]
            print(json.dumps({"restored": len(w.actions) - len(kept), "kept": len(kept), "actions": w.actions},
                             ensure_ascii=False), flush=True)
            return 1 if kept else 0
        if args.once:
            result = w.tick()
            print(json.dumps(result, ensure_ascii=False, indent=1), flush=True)
            return 0
        return w.run()
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
