"""Domain-name sources: downloaded block lists (contract §5) and the
focus-dnslog activity log used to validate penalty domains (§6b)."""

import datetime
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request

from . import paths
from .config import normalize_host, under_any

MAX_DOWNLOAD = 20 * 1024 * 1024
FETCH_TIMEOUT = 30
RETRY_SECONDS = 3600
DNSLOG_CAP = 50 * 1024 * 1024
DNSLOG_WINDOW = 15 * 60


def parse_list(text: str, never_block=()) -> list:
    """Hostnames from a hosts-style or plain list, validated, sorted, deduped.

    Accepts `0.0.0.0 name`, `127.0.0.1 name` or a bare `name`; lines with
    any other IP are ignored.
    """
    never = set(never_block)
    names = set()
    for raw in text.splitlines():
        tokens = raw.split("#", 1)[0].split()
        if not tokens:
            continue
        if len(tokens) == 1:
            cands = tokens
        elif tokens[0] in ("0.0.0.0", "127.0.0.1"):
            cands = tokens[1:]
        else:
            continue
        for c in cands:
            n = normalize_host(c)
            if n and not under_any(n, never):
                names.add(n)
    return sorted(names)


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "focusd"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        if not resp.geturl().startswith("https://"):
            raise ValueError("redirected away from https")
        data = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("download larger than 20 MB")
    return data


class Lists:
    """Cached list entries, refreshed in background threads."""

    def __init__(self, log_event, on_update, download=_download):
        self.log_event = log_event
        self.on_update = on_update     # called (no args) after a successful update
        self.download = download
        self.entries = {}              # name -> sorted list of hostnames
        self.meta = {}                 # name -> {url, updated, entries, sha256}
        self.version = 0
        self._lock = threading.Lock()
        self._inflight = set()
        self._last_try = {}
        self._force = set()

    def _files(self, name):
        d = paths.lists_dir()
        return os.path.join(d, f"{name}.txt"), os.path.join(d, f"{name}.meta.json")

    def _read_cache(self, name):
        """(raw txt bytes, meta dict) or (None, None) if there is no usable cache."""
        txt_path, meta_path = self._files(name)
        try:
            raw = paths.read_bytes(txt_path)
        except OSError as e:
            print(f"focusd: list cache {name} unreadable: {e}", file=sys.stderr)
            return None, None
        try:
            meta = json.loads(paths.read_bytes(meta_path) or b"{}")
        except (OSError, ValueError):
            meta = {}
        return raw, meta

    def load_cache(self, cfg) -> None:
        for name in cfg.lists:
            if name in self.entries:
                continue
            raw, meta = self._read_cache(name)
            if raw is None:
                continue
            if not isinstance(meta, dict):
                meta = {}
            if meta.get("sha256") != hashlib.sha256(raw).hexdigest():
                self._force.add(name)   # tampered or torn cache: refetch soon
            names = sorted({n for n in map(normalize_host, raw.decode("utf-8", "replace").split()) if n})
            self.meta[name] = {**meta, "entries": len(names)}
            self.entries = {**self.entries, name: names}
            self.version += 1

    def _stale(self, spec, now) -> bool:
        m = self.meta.get(spec.name)
        if m is None or spec.name in self._force or m.get("url") != spec.url:
            return True
        updated = m.get("updated")
        return not isinstance(updated, (int, float)) or now - updated >= spec.refresh_days * 86400

    def refresh(self, cfg, now: float) -> None:
        """Start background fetches for missing or stale lists."""
        for spec in cfg.lists.values():
            with self._lock:
                if spec.name in self._inflight or not self._stale(spec, now):
                    continue
                if now - self._last_try.get(spec.name, -1e12) < RETRY_SECONDS:
                    continue
                self._inflight.add(spec.name)
                self._last_try[spec.name] = now
            threading.Thread(target=self._fetch, args=(spec, list(cfg.never_block)),
                             name=f"list-{spec.name}", daemon=True).start()

    def _fetch(self, spec, never_block) -> None:
        try:
            names = parse_list(self.download(spec.url).decode("utf-8", "replace"), never_block)
            if not spec.min_entries <= len(names) <= spec.max_entries:
                raise ValueError(f"{len(names)} entries, outside "
                                 f"[{spec.min_entries}, {spec.max_entries}]")
            txt = ("\n".join(names) + "\n").encode()
            meta = {"url": spec.url, "updated": time.time(), "entries": len(names),
                    "sha256": hashlib.sha256(txt).hexdigest()}
            paths.ensure_dir(paths.lists_dir())
            txt_path, meta_path = self._files(spec.name)
            paths.atomic_write(txt_path, txt)
            paths.atomic_write(meta_path, (json.dumps(meta) + "\n").encode())
        except Exception as e:  # network, size, parse, disk: keep the cache
            with self._lock:
                self._inflight.discard(spec.name)
            self.log_event("list_error", name=spec.name, error=str(e)[:300])
            return
        with self._lock:
            self._inflight.discard(spec.name)
            self._force.discard(spec.name)
            self.meta[spec.name] = meta
            self.entries = {**self.entries, spec.name: names}
            self.version += 1
        self.log_event("list_update", name=spec.name, entries=len(names))
        try:
            self.on_update()
        except Exception as e:
            print(f"focusd: reconcile after list update failed: {e}", file=sys.stderr)

    def status(self, cfg) -> dict:
        out = {}
        for name in cfg.lists:
            m = self.meta.get(name) or {}
            out[name] = {"entries": len(self.entries.get(name, ())),
                         "updated": m.get("updated")}
        return out


# ---------------------------------------------------------------- DNS log

def _dnslog_files(directory: str, now: float) -> list:
    days = set()
    for t in (now, now - 86400):
        days.add(datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d"))
        days.add(datetime.datetime.fromtimestamp(t, datetime.UTC).strftime("%Y-%m-%d"))
    return [os.path.join(directory, f"{d}.jsonl") for d in sorted(days, reverse=True)]


def dnslog_check(domain: str, now: float) -> str | None:
    """None if `domain` itself or `www.<domain>` was resolved in the last
    15 min, else the reason: "no_dnslog" or "not_in_dnslog". A name merely
    under the domain does not count (it would let a parent like ac.uk in)."""
    directory = paths.dnslog_dir()
    if not os.path.isdir(directory):
        return "no_dnslog"
    needle = domain.encode()
    wanted = {domain, "www." + domain}
    budget = DNSLOG_CAP
    for path in _dnslog_files(directory, now):
        if budget <= 0:
            break
        try:
            with open(path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                n = min(size, budget)
                f.seek(size - n)
                data = f.read(n)
        except OSError:
            continue
        budget -= len(data)
        for line in reversed(data.split(b"\n")):
            if needle not in line:
                continue
            try:
                ev = json.loads(line)
                t, name = float(ev["t"]), str(ev["name"]).lower().rstrip(".")
            except (ValueError, KeyError, TypeError):
                continue
            if now - DNSLOG_WINDOW <= t <= now + 60 and name in wanted:
                return None
    return "not_in_dnslog"
