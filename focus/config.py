"""Config loading and validation (contract §3).

load() never raises. It returns (config, errors, fatal):
- errors: human-readable problems; each becomes a `config_error` event.
- fatal: the file is unusable as a reload (missing, unparsable, or `mode`
  is not "light"). On reload the daemon keeps its previous config. On first
  start it uses `config` if there is one (everything that parsed, locked).
Bad entries (group ids, hostnames, regexes, values) are dropped or replaced
by defaults; the rest still loads.
"""

import re
import tomllib
from dataclasses import dataclass, field

GROUP_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_TLD = r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"  # starts with a letter: no IPs
HOST_RE = re.compile(rf"^(?:{_LABEL}\.)+{_TLD}$")
WATCH_MODES = ("off", "observe", "nudge", "enforce")
DEFAULT_JUDGE_COMMAND = ["claude"]


# ---------------------------------------------------------------- hostnames

def valid_hostname(name: str) -> bool:
    """Strict check on an already lowercased name (no localhost, no IPs)."""
    if not isinstance(name, str) or len(name) > 253 or not HOST_RE.match(name):
        return False
    return not (name.startswith("localhost.") or name.endswith(".localhost"))


def normalize_host(name) -> str | None:
    """Lowercase, trim, drop a trailing dot; None if not a valid hostname."""
    if not isinstance(name, str):
        return None
    name = name.strip().lower().rstrip(".")
    return name if valid_hostname(name) else None


def is_under(name: str, domain: str) -> bool:
    """name equals domain or is a subdomain of it."""
    return name == domain or name.endswith("." + domain)


def under_any(name: str, domains) -> bool:
    """name equals or is under any entry of the set `domains`."""
    parts = name.split(".")
    return any(".".join(parts[i:]) in domains for i in range(len(parts)))


def overlaps_any(name: str, domains) -> bool:
    """name equals, is under, or is a parent of any entry of `domains`."""
    return under_any(name, domains) or any(d.endswith("." + name) for d in domains)


# Registrable-domain suffixes that must never be blocked as a whole. Two-label
# names like co.uk / ac.uk / com.au are caught by the rule in too_broad().
PUBLIC_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "blogspot.com", "wordpress.com", "herokuapp.com",
    "appspot.com", "netlify.app", "vercel.app", "pages.dev", "workers.dev", "web.app",
    "firebaseapp.com", "cloudfront.net", "amazonaws.com", "s3.amazonaws.com",
    "azurewebsites.net", "ngrok.io", "ngrok-free.app", "fly.dev", "onrender.com",
    "readthedocs.io", "substack.com", "medium.com", "tumblr.com",
})


def too_broad(d: str) -> bool:
    """A public suffix or a very short name: blocking it would hit unrelated sites."""
    labels = d.split(".")
    if len(labels) < 2 or d in PUBLIC_SUFFIXES:
        return True
    return len(labels) == 2 and len(labels[0]) <= 3 and len(labels[1]) == 2


def penalty_domain(raw, never_block):
    """Normalise a penalty domain: (domain, None) if usable, else (None, reason).

    Lowercased, URL parts and a leading www. stripped; rejected when invalid,
    too broad, or equal to / under / a parent of a never_block entry.
    """
    if not isinstance(raw, str):
        return None, "invalid"
    s = url_host(raw)
    if s.startswith("www."):
        s = s[4:]
    d = normalize_host(s)
    if d is None:
        return None, "invalid"
    if too_broad(d):
        return None, "too_broad"
    if overlaps_any(d, set(never_block)):
        return None, "never_block"
    return d, None


def url_host(pattern: str) -> str:
    """Host part of a Chromium URL-filter pattern ("youtube.com/embed" -> "youtube.com")."""
    s = pattern.strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0]
    s = s.rsplit("@", 1)[-1]
    if s.count(":") == 1:
        s = s.split(":", 1)[0]
    return s.lstrip("*").lstrip(".").rstrip(".")


# ---------------------------------------------------------------- schema

@dataclass
class Limits:
    max_unlock_minutes: int = 20
    daily_minutes: int = 40
    deny_cooldown_minutes: int = 5
    judge_calls_per_hour: int = 8
    max_followups: int = 3


@dataclass
class JudgeCfg:
    command: list = field(default_factory=lambda: list(DEFAULT_JUDGE_COMMAND))
    model: str = "sonnet"
    timeout_seconds: int = 90
    policy_file: str = "/etc/focus/judge.md"
    context_files: list = field(default_factory=list)


@dataclass
class BrowserCfg:
    policy_dirs: list = field(default_factory=lambda: [
        "/etc/chromium/policies/managed", "/etc/vivaldi/policies/managed"])
    disable_doh: bool = True
    force_google_safesearch: bool = True


@dataclass
class ListCfg:
    name: str
    url: str
    refresh_days: int = 7
    min_entries: int = 1000
    max_entries: int = 500000


@dataclass
class Group:
    id: str
    label: str
    enabled: bool = True
    unlockable: bool = True
    max_minutes: int | None = None
    daily_minutes: int | None = None
    hosts: list = field(default_factory=list)
    lists: list = field(default_factory=list)
    url_block: list = field(default_factory=list)
    url_allow: list = field(default_factory=list)
    webapp_classes: list = field(default_factory=list)
    title_patterns: list = field(default_factory=list)


@dataclass
class WatchCfg:
    mode: str = "observe"
    title_interval_seconds: int = 60
    penalty_minutes: int = 30
    max_active_penalties: int = 10


@dataclass
class Config:
    user: str = "theo"
    mode: str = "light"
    day_starts_at: tuple = (4, 0)
    reconcile_seconds: int = 30
    limits: Limits = field(default_factory=Limits)
    judge: JudgeCfg = field(default_factory=JudgeCfg)
    browser: BrowserCfg = field(default_factory=BrowserCfg)
    never_block: list = field(default_factory=list)
    lists: dict = field(default_factory=dict)    # name -> ListCfg, file order
    groups: dict = field(default_factory=dict)   # id -> Group, file order
    watch: WatchCfg = field(default_factory=WatchCfg)

    def cap(self, g: Group) -> int:
        """Effective per-grant cap for a group."""
        top = self.limits.max_unlock_minutes
        return min(g.max_minutes, top) if g.max_minutes is not None else top

    def daily(self, g: Group) -> int:
        return g.daily_minutes if g.daily_minutes is not None else self.limits.daily_minutes


# ---------------------------------------------------------------- loading

class _Reader:
    """Typed getters that record an error and fall back to a default."""

    def __init__(self):
        self.errors = []

    def err(self, msg):
        self.errors.append(msg)

    def table(self, data, key, where):
        v = data.get(key, {})
        if not isinstance(v, dict):
            self.err(f"{where}{key}: expected a table")
            return {}
        return v

    def int(self, t, key, default, where, lo=0, hi=10**9, optional=False):
        if key not in t:
            return default
        v = t[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            self.err(f"{where}{key}: expected an integer in [{lo}, {hi}], got {v!r}")
            return default
        return v

    def bool(self, t, key, default, where):
        v = t.get(key, default)
        if not isinstance(v, bool):
            self.err(f"{where}{key}: expected true/false, got {v!r}")
            return default
        return v

    def str(self, t, key, default, where, maxlen=200):
        v = t.get(key, default)
        if not isinstance(v, str) or not v or len(v) > maxlen:
            self.err(f"{where}{key}: expected a non-empty string, got {v!r}")
            return default
        return v

    def strlist(self, t, key, where):
        v = t.get(key, [])
        if not isinstance(v, list):
            self.err(f"{where}{key}: expected a list")
            return []
        out = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            else:
                self.err(f"{where}{key}: dropped non-string entry {item!r}")
        return out

    def hosts(self, t, key, where):
        out = []
        for h in self.strlist(t, key, where):
            n = normalize_host(h)
            if n is None:
                self.err(f"{where}{key}: dropped invalid hostname {h!r}")
            elif n not in out:
                out.append(n)
        return out

    def regexes(self, t, key, where):
        out = []
        for r in self.strlist(t, key, where):
            try:
                re.compile(r)
                out.append(r)
            except re.error as e:
                self.err(f"{where}{key}: dropped bad regex {r!r}: {e}")
        return out

    def urls(self, t, key, where):
        out = []
        for u in self.strlist(t, key, where):
            u = u.strip()
            if not u or len(u) > 1000 or any(c.isspace() or ord(c) < 32 for c in u):
                self.err(f"{where}{key}: dropped bad URL pattern {u!r}")
            elif u not in out:
                out.append(u)
        return out

    def paths(self, t, key, where):
        out = []
        for s in self.strlist(t, key, where):
            if s.startswith("/"):
                out.append(s)
            else:
                self.err(f"{where}{key}: dropped relative path {s!r}")
        return out


def load(path: str):
    """Read and validate a config file. Returns (Config|None, errors, fatal)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return None, [f"config file not found: {path}"], True
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return None, [f"cannot parse {path}: {e}"], True
    try:
        return from_dict(data)
    except Exception as e:  # a validator bug must not take the daemon down
        return None, [f"cannot validate {path}: {type(e).__name__}: {e}"], True


def from_dict(data: dict):
    r = _Reader()
    cfg = Config()
    fatal = False

    user = data.get("user", "theo")
    if isinstance(user, str) and USER_RE.match(user):
        cfg.user = user
    else:
        r.err(f"user: invalid user name {user!r}, using 'theo'")

    mode = data.get("mode", "light")
    if mode != "light":
        r.err(f"mode: v1 supports only 'light', got {mode!r}")
        fatal = True

    dsa = data.get("day_starts_at", "04:00")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", dsa) if isinstance(dsa, str) else None
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        cfg.day_starts_at = (int(m[1]), int(m[2]))
    else:
        r.err(f"day_starts_at: expected 'HH:MM', got {dsa!r}")
    cfg.reconcile_seconds = r.int(data, "reconcile_seconds", 30, "", lo=1, hi=3600)

    lim = r.table(data, "limits", "")
    w = "limits."
    cfg.limits = Limits(
        max_unlock_minutes=r.int(lim, "max_unlock_minutes", 20, w, lo=1, hi=1440),
        daily_minutes=r.int(lim, "daily_minutes", 40, w, hi=1440),
        deny_cooldown_minutes=r.int(lim, "deny_cooldown_minutes", 5, w, hi=1440),
        judge_calls_per_hour=r.int(lim, "judge_calls_per_hour", 8, w, hi=1000),
        max_followups=r.int(lim, "max_followups", 3, w, hi=20),
    )

    jt = r.table(data, "judge", "")
    w = "judge."
    command = jt.get("command", DEFAULT_JUDGE_COMMAND)
    if not (isinstance(command, list) and command
            and all(isinstance(c, str) and c for c in command)):
        r.err(f"judge.command: expected a non-empty list of strings, got {command!r}")
        command = list(DEFAULT_JUDGE_COMMAND)
    policy_file = r.str(jt, "policy_file", "/etc/focus/judge.md", w, maxlen=4096)
    if not policy_file.startswith("/"):
        r.err(f"judge.policy_file: must be absolute, got {policy_file!r}")
        policy_file = "/etc/focus/judge.md"
    cfg.judge = JudgeCfg(
        command=list(command),
        model=r.str(jt, "model", "sonnet", w),
        timeout_seconds=r.int(jt, "timeout_seconds", 90, w, lo=1, hi=140),  # clients wait 150 s
        policy_file=policy_file,
        context_files=r.paths(jt, "context_files", w),
    )

    bt = r.table(data, "browser", "")
    w = "browser."
    cfg.browser = BrowserCfg(
        policy_dirs=r.paths(bt, "policy_dirs", w) if "policy_dirs" in bt
        else BrowserCfg().policy_dirs,
        disable_doh=r.bool(bt, "disable_doh", True, w),
        force_google_safesearch=r.bool(bt, "force_google_safesearch", True, w),
    )

    cfg.never_block = r.hosts(r.table(data, "safety", ""), "never_block", "safety.")
    never = set(cfg.never_block)

    for name, lt in r.table(data, "lists", "").items():
        w = f"lists.{name}."
        if not GROUP_ID_RE.match(name) or not isinstance(lt, dict):
            r.err(f"lists.{name}: dropped (bad name or not a table)")
            continue
        url = lt.get("url")
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2000:
            r.err(f"{w}url: expected an https:// URL, list dropped")
            continue
        lo = r.int(lt, "min_entries", 1000, w, hi=10**7)
        cfg.lists[name] = ListCfg(
            name=name, url=url,
            refresh_days=r.int(lt, "refresh_days", 7, w, lo=1, hi=365),
            min_entries=lo,
            max_entries=r.int(lt, "max_entries", 500000, w, lo=max(lo, 1), hi=10**7),
        )

    for gid, gt in r.table(data, "groups", "").items():
        w = f"groups.{gid}."
        if not GROUP_ID_RE.match(gid) or not isinstance(gt, dict):
            r.err(f"groups.{gid}: dropped (bad group id or not a table)")
            continue
        hosts = []
        for h in r.hosts(gt, "hosts", w):
            if under_any(h, never):
                r.err(f"{w}hosts: {h} is covered by safety.never_block, not blocked")
            else:
                hosts.append(h)
        lists = []
        for ln in r.strlist(gt, "lists", w):
            if ln in cfg.lists:
                lists.append(ln)
            else:
                r.err(f"{w}lists: unknown list {ln!r} dropped")
        label = gt.get("label", gid)
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            r.err(f"{w}label: expected a short string")
            label = gid
        cfg.groups[gid] = Group(
            id=gid, label=label,
            enabled=r.bool(gt, "enabled", True, w),
            unlockable=r.bool(gt, "unlockable", True, w),
            max_minutes=r.int(gt, "max_minutes", None, w, lo=1, hi=1440),
            daily_minutes=r.int(gt, "daily_minutes", None, w, hi=1440),
            hosts=hosts, lists=lists,
            url_block=r.urls(gt, "url_block", w),
            url_allow=r.urls(gt, "url_allow", w),
            webapp_classes=r.regexes(gt, "webapp_classes", w),
            title_patterns=r.regexes(gt, "title_patterns", w),
        )

    wt = r.table(data, "watch", "")
    w = "watch."
    wmode = wt.get("mode", "observe")
    if wmode not in WATCH_MODES:
        r.err(f"watch.mode: unknown mode {wmode!r}, using 'observe'")
        wmode = "observe"
    cfg.watch = WatchCfg(
        mode=wmode,
        title_interval_seconds=r.int(wt, "title_interval_seconds", 60, w, lo=1, hi=86400),
        penalty_minutes=r.int(wt, "penalty_minutes", 30, w, lo=1, hi=1440),
        max_active_penalties=r.int(wt, "max_active_penalties", 10, w, hi=1000),
    )
    return cfg, r.errors, fatal
