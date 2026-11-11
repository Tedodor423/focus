"""Filesystem layout and low-level file helpers.

This is the only module that applies the FOCUS_ROOT test prefix (contract §2):
every absolute path the daemon touches, including paths read from the config,
goes through p(). With FOCUS_ROOT set, or when not running as root, the daemon
is "unprivileged": it skips chown, `resolvectl flush-caches` and running the
judge as the config user (Popen user=/group=).
"""

import os
import stat
import tempfile


def root() -> str:
    """The test prefix ("" in production)."""
    return os.environ.get("FOCUS_ROOT", "").rstrip("/")


def p(path: str) -> str:
    """FOCUS_ROOT + an absolute path."""
    if not path.startswith("/"):
        raise ValueError(f"not an absolute path: {path!r}")
    return root() + path


def privileged() -> bool:
    """True only for the real root daemon (no FOCUS_ROOT, euid 0)."""
    return root() == "" and os.geteuid() == 0


# Fixed locations (functions, so tests can change FOCUS_ROOT per process).
def config_file() -> str:
    return p("/etc/focus/config.toml")


def hosts_file() -> str:
    return p("/etc/hosts")


def state_dir() -> str:
    return p("/var/lib/focus")


def state_file() -> str:
    return p("/var/lib/focus/state.json")


def events_file() -> str:
    return p("/var/lib/focus/events.jsonl")


def usage_file() -> str:
    return p("/var/lib/focus/usage.json")


def learned_file() -> str:
    return p("/var/lib/focus/learned.json")


def lists_dir() -> str:
    return p("/var/lib/focus/lists")


def judge_dir() -> str:
    return p("/var/lib/focus/judge")


def dnslog_dir() -> str:
    return p("/var/log/focus/dns")


def run_dir() -> str:
    return p("/run/focusd")


def socket_file() -> str:
    return p("/run/focusd/focusd.sock")


def ensure_dir(path: str, mode: int = 0o755) -> None:
    """Create a directory (with parents) and give newly made ones `mode`."""
    missing = []
    d = path
    while d and d != "/" and not os.path.isdir(d):
        missing.append(d)
        d = os.path.dirname(d)
    for d in reversed(missing):
        try:
            os.mkdir(d, mode)
        except FileExistsError:
            continue
        os.chmod(d, mode)
        if privileged():
            os.chown(d, 0, 0)


def read_bytes(path: str) -> bytes | None:
    """File content, or None when the file does not exist."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


class UntrustedFile(Exception):
    """A file root must not read and forward (symlink, not regular, not root's)."""


def read_trusted(path: str, limit: int, require_root: bool | None = None) -> bytes:
    """Read at most `limit` bytes of a regular file without following a symlink.

    When running as root, the file must also be owned by root. Raises
    FileNotFoundError when missing and UntrustedFile for everything else.
    """
    if require_root is None:
        require_root = privileged()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as e:
        reason = "is a symlink" if e.errno == 40 else e.strerror
        raise UntrustedFile(f"{path}: {reason}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UntrustedFile(f"{path}: not a regular file")
        if require_root and st.st_uid != 0:
            raise UntrustedFile(f"{path}: not owned by root")
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes, mode: int = 0o644) -> None:
    """Temp file in the same dir, fsync, chmod, chown root (root only), rename.

    Never truncates the target in place.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".focus-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
            if privileged():
                os.fchown(f.fileno(), 0, 0)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
