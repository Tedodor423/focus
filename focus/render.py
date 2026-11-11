"""Rendering of the /etc/hosts block and the browser policy, plus the writer
that applies them (contract §4).

Everything here is deterministic: the same config, locked groups, list
entries and penalty domains always give the same bytes.
"""

import json
import os
import subprocess
import sys

from . import extension, paths
from .config import overlaps_any, under_any

BEGIN = "# BEGIN focus — managed by focusd, manual edits are reverted"
END = "# END focus"
POLICY_NAME = "focus.json"


class HostsError(Exception):
    """/etc/hosts is in a state we refuse to rewrite."""


# ---------------------------------------------------------------- hosts

def host_lines(cfg, locked, list_entries, penalty_domains) -> list:
    """Lines for the managed block (without markers).

    Groups in config order: their `hosts` get 0.0.0.0 and :: lines, their
    lists' entries (already sorted) get 0.0.0.0 only. Then penalty domains,
    each with www. Deduplicated per line; never_block applied throughout.
    """
    never = set(cfg.never_block)
    out, seen = [], set()

    def add(ip, name):
        line = f"{ip} {name}"
        if line not in seen and not under_any(name, never):
            seen.add(line)
            out.append(line)

    for g in cfg.groups.values():
        if g.id not in locked:
            continue
        for h in g.hosts:
            add("0.0.0.0", h)
            add("::", h)
        for ln in g.lists:
            for name in list_entries.get(ln, ()):
                add("0.0.0.0", name)
    for d in penalty_domains:
        if overlaps_any(d, never):
            continue
        for name in _with_www(d):
            add("0.0.0.0", name)
            add("::", name)
    return out


def _with_www(d: str) -> list:
    return [d] if d.startswith("www.") else [d, "www." + d]


def render_block(lines) -> bytes:
    return "".join(f"{s}\n" for s in [BEGIN, *lines, END]).encode()


def group_host_count(cfg, g, list_entries) -> int:
    never = set(cfg.never_block)
    names = set(g.hosts)
    for ln in g.lists:
        names.update(list_entries.get(ln, ()))
    return sum(1 for n in names if not under_any(n, never))


def _is_begin(line: bytes) -> bool:
    return line.strip().startswith(b"# BEGIN focus")


def _is_end(line: bytes) -> bool:
    return line.strip().startswith(b"# END focus")


def split_hosts(content: bytes):
    """(before, block, after) as lists of raw lines; block is None if absent."""
    lines = content.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if _is_begin(line)]
    if len(begins) > 1:
        raise HostsError("more than one '# BEGIN focus' marker")
    if not begins:
        return lines, None, []
    b = begins[0]
    for e in range(b + 1, len(lines)):
        if _is_end(lines[e]):
            return lines[:b], lines[b:e + 1], lines[e + 1:]
    raise HostsError("'# BEGIN focus' without '# END focus'")


def managed_part(content: bytes | None) -> bytes | None:
    """The current managed block of a hosts file (None if absent/unreadable)."""
    if content is None:
        return None
    try:
        _, block, _ = split_hosts(content)
    except HostsError:
        return None
    return b"".join(block) if block is not None else None


def splice_hosts(current: bytes | None, block: bytes) -> bytes:
    """Cut out the old block, append the new one; keep the rest byte for byte."""
    before, _, after = split_hosts(current or b"")
    kept = before + after
    outside = b"".join(kept)
    if outside and not outside.endswith((b"\n", b"\r")):
        outside += b"\n"
    result = outside + block
    # Self-check: one block, at the end, and every other line still in order.
    r_before, r_block, r_after = split_hosts(result)
    strip = [line.rstrip(b"\r\n") for line in kept]
    if (r_block is None or r_after or b"".join(r_block) != block
            or [line.rstrip(b"\r\n") for line in r_before] != strip):
        raise HostsError("self-check failed: non-managed lines would change")
    return result


# ---------------------------------------------------------------- policy

def policy_json(cfg, locked, penalty_domains) -> bytes:
    block, allow = [], []
    for g in cfg.groups.values():
        if g.id in locked:
            block += [u for u in g.url_block if u not in block]
            allow += [u for u in g.url_allow if u not in allow]
    never = set(cfg.never_block)
    for d in penalty_domains:
        if overlaps_any(d, never):
            continue
        # A leading dot means "this exact host" in Chromium's URL filter format,
        # so a penalty never blocks the domain's other subdomains.
        block += [f".{n}" for n in _with_www(d) if f".{n}" not in block]
    obj = {"URLBlocklist": block, "URLAllowlist": allow}
    if cfg.browser.disable_doh:
        obj["DnsOverHttpsMode"] = "off"
    if cfg.browser.force_google_safesearch:
        obj["ForceGoogleSafeSearch"] = True
    built = extension.installed()
    if built:
        ext_id, update_url = built
        obj["ExtensionInstallForcelist"] = [f"{ext_id};{update_url}"]
        # Incognito refuses to load pages until the extension is allowed there.
        obj["MandatoryExtensionsForIncognitoNavigation"] = [ext_id]
        obj["3rdparty"] = {"extensions": {ext_id: {"block": block, "allow": allow}}}
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode()


# ---------------------------------------------------------------- writer

def _ensure_policy_dir(d: str) -> bool:
    """Create a missing policy dir (0755, with parents) when the browser's
    config root exists: /etc for /etc/vivaldi/policies/managed."""
    if os.path.isdir(d):
        return True
    if not os.path.isdir(os.path.dirname(os.path.dirname(os.path.dirname(d)))):
        return False
    paths.ensure_dir(d, 0o755)
    return True


class Writer:
    """Writes the desired files, detects external edits (restores) and keeps
    the list of persistent write problems for status.health."""

    def __init__(self, log_event):
        self.log_event = log_event
        self.last_known = {}       # path -> managed content we last wrote/saw
        self.hosts_problem = None  # health line while hosts can't be written
        self.policy_problems = {}  # path -> health line
        self._hosts_err = None     # last logged hosts_error message
        self._cache_key = None
        self._cache_block = None

    def health(self) -> list:
        return ([self.hosts_problem] if self.hosts_problem else []) + \
            [self.policy_problems[k] for k in sorted(self.policy_problems)]

    def block(self, cfg, locked, lists, penalty_domains) -> bytes:
        key = (lists.version, frozenset(locked), tuple(penalty_domains))
        if self._cache_key is None or self._cache_key[0] is not cfg or self._cache_key[1] != key:
            lines = host_lines(cfg, locked, lists.entries, penalty_domains)
            self._cache_block = render_block(lines)
            self._cache_key = (cfg, key)
        return self._cache_block

    def apply(self, cfg, block: bytes, policy: bytes) -> bool:
        """Bring all files to the desired content. Returns True if hosts changed."""
        hosts_changed = self._apply_hosts(block)
        wanted = [os.path.join(paths.p(d), POLICY_NAME) for d in cfg.browser.policy_dirs]
        for path in list(self.policy_problems):
            if path not in wanted:
                del self.policy_problems[path]
        for path in wanted:
            self._apply_policy(path, policy)
        if hosts_changed and paths.privileged():
            try:
                subprocess.run(["resolvectl", "flush-caches"], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
        return hosts_changed

    def _restore_check(self, path, managed_now):
        if path in self.last_known and managed_now != self.last_known[path]:
            self.log_event("restore", file=path)

    def _hosts_failed(self, message: str, health: str) -> bool:
        """Log hosts_error whenever the message changes; keep the health line."""
        self.hosts_problem = health
        if self._hosts_err != message:
            self._hosts_err = message
            self.log_event("hosts_error", error=message)
        return False

    def _apply_hosts(self, block: bytes) -> bool:
        path = paths.hosts_file()
        try:
            current = paths.read_bytes(path)
            desired = splice_hosts(current, block)
        except HostsError as e:
            return self._hosts_failed(str(e), f"hosts: refusing to write ({e})")
        except OSError as e:
            return self._hosts_failed(f"cannot read {path}: {e}", f"hosts: cannot read {path}: {e}")
        if current != desired:
            self._restore_check(path, managed_part(current))
            try:
                paths.atomic_write(path, desired)
            except OSError as e:
                return self._hosts_failed(f"write failed: {e}", f"hosts: cannot write {path}: {e}")
        self.last_known[path] = block
        self.hosts_problem = self._hosts_err = None
        return current != desired

    def _apply_policy(self, path: str, policy: bytes) -> None:
        d = os.path.dirname(path)
        try:
            if not _ensure_policy_dir(d):
                raise OSError(f"{d} is missing and cannot be created")
            current = paths.read_bytes(path)
            if current != policy:
                self._restore_check(path, current)
                paths.atomic_write(path, policy)
            self.last_known[path] = policy
            self.policy_problems.pop(path, None)
        except OSError as e:
            problem = f"policy: cannot write {path}: {e}"
            if self.policy_problems.get(path) != problem:
                print(f"focusd: {problem}", file=sys.stderr)
            self.policy_problems[path] = problem
