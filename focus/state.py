"""Persistent state (state.json), the append-only event log (events.jsonl)
and day arithmetic (contract §8).

state.json:
  {"unlocks": {"youtube": {"since", "until", "minutes", "reason"}},
   "cooldowns": {"youtube": <epoch of the last deny>},
   "penalties": [ {id, since, until, domain, app, title, description, source, reason} ],
   "appeal_cooldowns": {"p1a2b3c4": <epoch of the last denied appeal>}}
"""

import datetime
import json
import os
import sys
import threading
import time

from . import paths

KEEP_RECENT_SECONDS = 2 * 86400   # in-memory window of events (covers "today")
TAIL_SCAN_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------- days

def day_start(now: float, hm: tuple) -> float:
    """Epoch of the most recent local `HH:MM` at or before `now`."""
    lt = datetime.datetime.fromtimestamp(now)
    start = lt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if start > lt:
        start -= datetime.timedelta(days=1)
    return start.timestamp()


def next_day_start(now: float, hm: tuple) -> float:
    start = datetime.datetime.fromtimestamp(day_start(now, hm))
    return (start + datetime.timedelta(days=1)).timestamp()


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


# ---------------------------------------------------------------- state.json

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class State:
    def __init__(self):
        self.unlocks = {}
        self.cooldowns = {}
        self.penalties = []
        self.appeal_cooldowns = {}

    def to_json(self) -> dict:
        return {"unlocks": self.unlocks, "cooldowns": self.cooldowns,
                "penalties": self.penalties, "appeal_cooldowns": self.appeal_cooldowns}

    def save(self, path: str) -> None:
        """Atomic write, mode 0600 root: it holds unlock reasons and window
        titles, and nothing user-side reads it (everything goes via the socket)."""
        data = json.dumps(self.to_json(), indent=1, ensure_ascii=False) + "\n"
        paths.atomic_write(path, data.encode(), mode=0o600)

    @classmethod
    def load(cls, path: str):
        """Returns (State, error or None). A missing file is not an error."""
        st = cls()
        raw = paths.read_bytes(path)
        if raw is None:
            return st, None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError as e:
            return st, f"state.json unreadable, starting empty: {e}"
        def table(key):
            v = data.get(key)
            return v if isinstance(v, dict) else {}

        for gid, u in table("unlocks").items():
            if isinstance(u, dict) and _num(u.get("since")) and _num(u.get("until")):
                minutes = u.get("minutes")
                st.unlocks[gid] = {"since": float(u["since"]), "until": float(u["until"]),
                                   "minutes": int(minutes) if _num(minutes) else 0,
                                   "reason": str(u.get("reason") or "")}
        for key, attr in (("cooldowns", "cooldowns"), ("appeal_cooldowns", "appeal_cooldowns")):
            for k, v in table(key).items():
                if _num(v):
                    getattr(st, attr)[k] = float(v)
        pens = data.get("penalties")
        for p in pens if isinstance(pens, list) else []:
            if (isinstance(p, dict) and isinstance(p.get("id"), str)
                    and _num(p.get("since")) and _num(p.get("until"))):
                st.penalties.append(p)
        return st, None


# ---------------------------------------------------------------- events.jsonl

def reverse_lines(path: str, max_bytes: int = TAIL_SCAN_BYTES):
    """Yield non-empty lines from the end of a file, newest first."""
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        stop = max(0, pos - max_bytes)
        buf = b""
        while pos > stop:
            n = min(65536, pos - stop)
            pos -= n
            f.seek(pos)
            buf = f.read(n) + buf
            parts = buf.split(b"\n")
            buf = parts[0]
            for line in reversed(parts[1:]):
                if line.strip():
                    yield line
        if pos == 0 and buf.strip():
            yield buf


def _parse(line: bytes):
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) and _num(ev.get("t")) else None


EVENTS_MODE = 0o640   # root:<user>: the log holds window titles


class EventLog:
    """Append-only JSONL with fsync; keeps the last two days in memory.

    The file is 0640 and, when running as root, owned by root:<gid> (`gid`
    set by the daemon), so only root and Theo can read it.
    """

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.recent = []
        self.gid = None

    def _secure_fd(self, fd) -> None:
        st = os.fstat(fd)
        if st.st_mode & 0o7777 != EVENTS_MODE:
            os.fchmod(fd, EVENTS_MODE)
        if paths.privileged() and self.gid is not None and (st.st_uid, st.st_gid) != (0, self.gid):
            os.fchown(fd, 0, self.gid)

    def secure(self) -> None:
        """Fix mode/owner of an existing log (called at startup)."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return
        try:
            self._secure_fd(fd)
        finally:
            os.close(fd)

    def open(self, now: float):
        """Load recent events; return the type of the last line ("" if the
        last line is unparsable, None if the log is empty)."""
        last_type = None
        first = True
        cutoff = now - KEEP_RECENT_SECONDS
        loaded = []
        for line in reverse_lines(self.path):
            ev = _parse(line)
            if first:
                last_type = ev.get("type", "") if ev else ""
                first = False
            if ev is None:
                continue
            if ev["t"] < cutoff - 3600:
                break
            if ev["t"] >= cutoff:
                loaded.append(ev)
        self.recent = loaded[::-1]
        raw_end = b""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    raw_end = f.read(1)
        except FileNotFoundError:
            pass
        if raw_end and raw_end != b"\n":
            self._write(b"\n")   # never glue a new event onto a torn line
        return last_type

    def _write(self, data: bytes) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                     EVENTS_MODE)
        try:
            self._secure_fd(fd)
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(self, type_: str, **fields) -> dict:
        ev = {"t": time.time(), "type": type_, **fields}
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            try:
                self._write(line.encode())
            except OSError as e:
                print(f"focusd: cannot append event: {e}", file=sys.stderr)
            self.recent.append(ev)
        return ev

    def prune(self, now: float) -> None:
        cutoff = now - KEEP_RECENT_SECONDS
        with self.lock:
            if self.recent and self.recent[0]["t"] < cutoff:
                self.recent = [e for e in self.recent if e["t"] >= cutoff]

    def since(self, t: float, types=None) -> list:
        """Events from memory with ev.t >= t (t must be within two days)."""
        with self.lock:
            return [e for e in self.recent
                    if e["t"] >= t and (types is None or e.get("type") in types)]

    def tail(self, limit: int, since=None, types=None) -> list:
        """The last `limit` matching events from disk, oldest first."""
        out = []
        for line in reverse_lines(self.path):
            ev = _parse(line)
            if ev is None:
                continue
            if since is not None and ev["t"] < since:
                if ev["t"] < since - 300:
                    break
                continue
            if types is not None and ev.get("type") not in types:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out[::-1]
