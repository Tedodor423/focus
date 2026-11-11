#!/bin/bash
# focus: root part of the install (light mode v1). Idempotent: run it again to update.
#
#   pkexec bash /home/theo/nextcloud/code/focus/install.sh [--no-start]
#   pkexec bash /home/theo/nextcloud/code/focus/install.sh --uninstall [--purge] [--force]
#                                              (uninstall.sh does the same)
# Then, as theo:  bash /home/theo/nextcloud/code/focus/install-user.sh
#
# Installs (ARCHITECTURE.md §2):
#   /usr/local/lib/focus/focus/*.py   the package          /usr/local/lib/focus/focus-dnslog
#   /usr/local/bin/focus, focus-watch /usr/local/sbin/focusd   (launchers)
#   /etc/focus/config.toml, judge.md  (a changed config is backed up to /var/lib/focus/backups/)
#                                     from etc/config.toml, etc/judge.md (git-ignored local copies),
#                                     or etc/*.example.* with a warning when those don't exist
#   /etc/systemd/system/focusd.service, focus-dnslog.service   (enabled + restarted)
#   /etc/systemd/user/focus-watch.service                      (systemctl --global enable)
#   /var/lib/focus/{lists,judge}/, /var/log/focus/dns/ (root:<user> 0750)
#   /var/lib/focus/hosts.orig         one-time copy of /etc/hosts, never overwritten
#   /var/lib/focus/extension/         the signed browser extension and its machine-local key
# It never edits /etc/hosts or the browser policy dirs itself (focusd does, and
# only its own block and focus.json; Omarchy's color.json/omarchy.json are never
# touched). Ollama is only checked, never installed or changed.
#
# --no-start   install the files and daemon-reload, but don't enable/start units.
#
# Test prefix: FOCUS_ROOT=/some/tmp/dir (or DESTDIR=...) installs everything under
# that directory, unprivileged, with no chown, systemctl or resolvectl. Commands
# that would run are printed as "would run: ...".
set -euo pipefail
umask 022

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=${FOCUS_ROOT:-${DESTDIR:-}}
OLLAMA_URL=${FOCUS_OLLAMA_URL:-http://127.0.0.1:11434}

ACTION=install NO_START=0 PURGE=0 FORCE=0
for arg in "$@"; do
  case $arg in
    --uninstall) ACTION=uninstall ;;
    --no-start) NO_START=1 ;;
    --purge) PURGE=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "focus install: unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; WARNINGS=$((WARNINGS + 1)); }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
WARNINGS=0

# ---------------------------------------------------------------- where to install
if [ -n "$ROOT" ]; then
  case $ROOT in /*) ;; *) die "FOCUS_ROOT must be an absolute path" ;; esac
  ROOT=${ROOT%/}
  [ -n "$ROOT" ] || die "FOCUS_ROOT=/ is not a test prefix; unset it for a real install"
  [ -d "$ROOT" ] || die "FOCUS_ROOT $ROOT does not exist"
  REAL=0
else
  [ "$(id -u)" -eq 0 ] || die "the root part must run as root: pkexec bash $REPO/$(basename "${BASH_SOURCE[0]}")${1:+ $*}"
  REAL=1
fi

LIB=$ROOT/usr/local/lib/focus
BIN=$ROOT/usr/local/bin
SBIN=$ROOT/usr/local/sbin
ETC=$ROOT/etc/focus
VAR=$ROOT/var/lib/focus
LOG=$ROOT/var/log/focus
SYSUNITS=$ROOT/etc/systemd/system
USERUNITS=$ROOT/etc/systemd/user
HOSTS=$ROOT/etc/hosts

# Run a system command for real, or print it under a test prefix.
run() {
  if [ "$REAL" -eq 1 ]; then "$@"; else echo "would run: $*"; fi
}

# ---------------------------------------------------------------- uninstall
uninstall() {
  if [ "$REAL" -eq 1 ] && [ "$FORCE" -eq 0 ] && pgrep -f '/usr/local/bin/focus-watch' >/dev/null 2>&1; then
    die "focus-watch is still running. Run 'bash $REPO/uninstall-user.sh' as theo first (it un-freezes and restores windows), or pass --force."
  fi
  local rc=0

  say "stopping and disabling the services"
  run systemctl disable --now focusd.service focus-dnslog.service || true
  run systemctl --global disable focus-watch.service || true

  say "removing the focus block from /etc/hosts"
  # Same rules as focusd (focus.render.split_hosts): one "# BEGIN focus" line, the
  # block ends at the first "# END focus" after it; stray END lines elsewhere are
  # kept. focusd's own function is used when importable (installed copy first).
  python3 -I - "$HOSTS" "$LIB" "$REPO" <<'PY' || rc=1
import os, sys, tempfile
sys.dont_write_bytecode = True
path, libs = sys.argv[1], sys.argv[2:]

def is_begin(l): return l.strip().startswith(b"# BEGIN focus")
def is_end(l): return l.strip().startswith(b"# END focus")

class Malformed(Exception):
    pass

def split_replica(content):
    lines = content.splitlines(keepends=True)
    begins = [i for i, l in enumerate(lines) if is_begin(l)]
    if len(begins) > 1:
        raise Malformed("more than one '# BEGIN focus' marker")
    if not begins:
        return lines, None, []
    b = begins[0]
    for e in range(b + 1, len(lines)):
        if is_end(lines[e]):
            return lines[:b], lines[b:e + 1], lines[e + 1:]
    raise Malformed("'# BEGIN focus' without '# END focus'")

split, errors, source = split_replica, (Malformed,), "built-in copy of focusd's rules"
for lib in libs:
    if not os.path.isfile(os.path.join(lib, "focus", "render.py")):
        continue
    sys.path.insert(0, lib)
    try:
        from focus import render
        split, errors, source = render.split_hosts, (render.HostsError, Malformed), f"focusd's split_hosts ({lib})"
        break
    except Exception:
        for m in [m for m in sys.modules if m == "focus" or m.startswith("focus.")]:
            del sys.modules[m]
    finally:
        sys.path.remove(lib)

try:
    data = open(path, "rb").read()
except FileNotFoundError:
    print(f"  {path} missing, nothing to do"); sys.exit(0)
try:
    before, block, after = split(data)
except errors as e:
    lines = data.splitlines()
    begins = [i + 1 for i, l in enumerate(lines) if is_begin(l)]
    ends = [i + 1 for i, l in enumerate(lines) if is_end(l)]
    print(f"  {path} is malformed: {e}.\n"
          f"  '# BEGIN focus' on line(s) {begins or 'none'}, '# END focus' on line(s) {ends or 'none'}.\n"
          f"  Left untouched. Fix by hand: keep one BEGIN … END block and delete it, or start from\n"
          f"  /var/lib/focus/hosts.orig (the copy taken at install time).", file=sys.stderr)
    sys.exit(1)
if block is None:
    print("  no focus block"); sys.exit(0)
out = b"".join(before + after)
st = os.stat(path)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".hosts.focus.")
try:
    with os.fdopen(fd, "wb") as f:
        f.write(out); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)
    os.rename(tmp, path)
except BaseException:
    try: os.unlink(tmp)
    except OSError: pass
    raise
print(f"  removed {len(block)} lines (using {source})")
PY

  say "removing focus.json from the browser policy dirs (only that file)"
  # browser.policy_dirs from the installed config (FOCUS_ROOT-prefixed like focusd
  # does), plus the two defaults in case an older config listed them.
  local d
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    if [ -f "$d/focus.json" ] && [ ! -L "$d/focus.json" ]; then
      rm -f -- "$d/focus.json"; echo "  removed $d/focus.json"
    fi
  done < <(python3 -I - "$ETC/config.toml" "$ROOT" <<'PY'
import sys, tomllib
cfg, root = sys.argv[1], sys.argv[2]
dirs = []
try:
    with open(cfg, "rb") as f:
        v = tomllib.load(f).get("browser", {}).get("policy_dirs")
    if isinstance(v, list):
        dirs = [d for d in v if isinstance(d, str) and d.startswith("/") and "\n" not in d and ".." not in d.split("/")]
except (OSError, ValueError):
    pass
seen = set()
for d in dirs + ["/etc/chromium/policies/managed", "/etc/vivaldi/policies/managed"]:
    p = root + d.rstrip("/")
    if p not in seen:
        seen.add(p)
        print(p)
PY
)
  run resolvectl flush-caches || true

  say "removing installed files"
  if [ -f "$ETC/config.toml" ] && [ "$PURGE" -eq 0 ]; then
    install -d -m 0755 "$VAR/backups"
    cp -p -- "$ETC/config.toml" "$VAR/backups/config.toml.uninstalled.$(date +%Y%m%d%H%M%S)"
  fi
  rm -rf -- "${LIB:?}"
  rm -f -- "$BIN/focus" "$BIN/focus-watch" "$SBIN/focusd"
  rm -f -- "$ETC/config.toml" "$ETC/judge.md"
  rmdir -- "$ETC" 2>/dev/null || true
  rm -f -- "$SYSUNITS/focusd.service" "$SYSUNITS/focus-dnslog.service" "$USERUNITS/focus-watch.service"
  run systemctl daemon-reload || true

  if [ "$PURGE" -eq 1 ]; then
    say "purging state and logs"
    rm -rf -- "${VAR:?}" "${LOG:?}"
  else
    echo "  kept $VAR (state, events.jsonl, hosts.orig, backups, extension key) and $LOG (DNS log); --purge removes them"
  fi
  [ "$rc" -eq 0 ] && say "focus root part uninstalled" || warn "uninstall finished with errors (see above)"
  return "$rc"
}

if [ "$ACTION" = uninstall ]; then
  uninstall
  exit $?
fi

# ---------------------------------------------------------------- preflight
say "checking the repo ($REPO)"
# etc/config.toml and etc/judge.md are local, git-ignored copies with your real
# values; a fresh clone only has the tracked etc/*.example.* files.
CONFIG_SRC=etc/config.toml
JUDGE_SRC=etc/judge.md
if [ ! -f "$REPO/$CONFIG_SRC" ] && [ -f "$REPO/etc/config.example.toml" ]; then
  CONFIG_SRC=etc/config.example.toml
  warn "using the example config; copy etc/config.example.toml to etc/config.toml and edit it"
fi
if [ ! -f "$REPO/$JUDGE_SRC" ] && [ -f "$REPO/etc/judge.example.md" ]; then
  JUDGE_SRC=etc/judge.example.md
  warn "using the example judge policy; copy etc/judge.example.md to etc/judge.md and edit it"
fi
missing=()
for f in focus/__init__.py focus/cli.py focus/daemon.py focus/watch.py \
         bin/focus bin/focusd bin/focus-watch dnslog/focus-dnslog \
         "$CONFIG_SRC" "$JUDGE_SRC" \
         systemd/focusd.service systemd/focus-dnslog.service systemd/focus-watch.service; do
  [ -f "$REPO/$f" ] || missing+=("$f")
done
[ ${#missing[@]} -eq 0 ] || die "missing in the repo: ${missing[*]}"

python3 -I -c 'import sys, tomllib; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || die "python3 >= 3.11 with tomllib is required"

shopt -s nullglob
PKG=("$REPO"/focus/*.py)
shopt -u nullglob

# Syntax-check every Python file without writing __pycache__ into the repo.
python3 -I - "${PKG[@]}" "$REPO/bin/focus" "$REPO/bin/focusd" "$REPO/bin/focus-watch" \
  "$REPO/dnslog/focus-dnslog" <<'PY' || die "Python syntax errors (see above); nothing installed"
import sys
bad = 0
for p in sys.argv[1:]:
    try:
        compile(open(p, "rb").read(), p, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        print(f"  {p}: {e}", file=sys.stderr); bad += 1
sys.exit(1 if bad else 0)
PY

# Parse the config: it must load, and we need user, the judge command and the model.
CFG_OUT=$(python3 -I - "$REPO/$CONFIG_SRC" <<'PY'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb"))
user = c.get("user", "")
cmd = (c.get("judge", {}).get("command") or [""])[0]
model = c.get("watch", {}).get("model", "")
mode = c.get("mode", "")
for v in (user, cmd, model, mode):
    if not isinstance(v, str) or "\t" in v or "\n" in v:
        sys.exit("bad value in config")
print(f"{user}\t{cmd}\t{model}\t{mode}")
PY
) || die "$CONFIG_SRC does not parse; nothing installed"
IFS=$'\t' read -r CFG_USER JUDGE_CMD WATCH_MODEL CFG_MODE <<<"$CFG_OUT"
[[ $CFG_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "config: bad user '$CFG_USER'"
[ "$CFG_MODE" = light ] || warn "config: mode is '$CFG_MODE'; v1 only accepts \"light\""
if [ "$REAL" -eq 1 ]; then
  id -u "$CFG_USER" >/dev/null 2>&1 || die "config user '$CFG_USER' does not exist"
fi

# ---------------------------------------------------------------- files
say "installing into ${ROOT:-/}"
install -d -m 0755 "$LIB" "$LIB/focus" "$BIN" "$SBIN" "$ETC" "$VAR" "$VAR/lists" "$VAR/judge" \
  "$SYSUNITS" "$USERUNITS"

# The package: copy *.py, drop modules that no longer exist in the repo, drop stale bytecode.
for f in "${PKG[@]}"; do install -m 0644 "$f" "$LIB/focus/"; done
for f in "$LIB"/focus/*.py; do
  [ -e "$f" ] || continue
  [ -f "$REPO/focus/$(basename "$f")" ] || { rm -f -- "$f"; echo "  removed stale $(basename "$f")"; }
done
rm -rf -- "$LIB/focus/__pycache__"

# Open tabs are only re-locked with this; everything else works without it.
if ext_id=$(FOCUS_ROOT="$ROOT" python3 -I -B -c 'import sys
sys.path.insert(0, sys.argv[1])
from focus import extension
print(extension.build(sys.argv[2]))' "$LIB" "$REPO/extension"); then
  echo "  browser extension built ($ext_id)"
else
  warn "could not build the browser extension (is openssl installed?); open tabs won't re-lock on their own"
fi

install -m 0755 "$REPO/dnslog/focus-dnslog" "$LIB/focus-dnslog"
install -m 0755 "$REPO/bin/focus" "$BIN/focus"
install -m 0755 "$REPO/bin/focus-watch" "$BIN/focus-watch"
install -m 0755 "$REPO/bin/focusd" "$SBIN/focusd"

stamp=$(date +%Y%m%d%H%M%S)
for f in config.toml judge.md; do
  case $f in config.toml) src=$REPO/$CONFIG_SRC ;; *) src=$REPO/$JUDGE_SRC ;; esac
  if [ -f "$ETC/$f" ] && ! cmp -s "$ETC/$f" "$src"; then
    install -d -m 0755 "$VAR/backups"
    cp -p -- "$ETC/$f" "$VAR/backups/$f.$stamp"
    echo "  $f changed; previous version saved as $VAR/backups/$f.$stamp"
  fi
  install -m 0644 "$src" "$ETC/$f"
done

install -m 0644 "$REPO/systemd/focusd.service" "$SYSUNITS/focusd.service"
install -m 0644 "$REPO/systemd/focus-dnslog.service" "$SYSUNITS/focus-dnslog.service"
install -m 0644 "$REPO/systemd/focus-watch.service" "$USERUNITS/focus-watch.service"

install -d -m 0755 "$LOG"
install -d -m 0750 "$LOG/dns"
if [ "$REAL" -eq 1 ]; then
  chown root:root "$LOG"
  chown "root:$(id -gn "$CFG_USER")" "$LOG/dns"
fi

# One-time backup of /etc/hosts, taken before focusd ever writes to it.
if [ -e "$VAR/hosts.orig" ]; then
  echo "  $VAR/hosts.orig already exists (kept)"
elif [ -f "$HOSTS" ]; then
  grep -q '^# BEGIN focus' "$HOSTS" && warn "/etc/hosts already has a focus block; hosts.orig will include it"
  install -m 0644 "$HOSTS" "$VAR/hosts.orig"
  echo "  saved /etc/hosts as $VAR/hosts.orig"
else
  warn "$HOSTS not found; no hosts.orig backup taken"
fi

# ---------------------------------------------------------------- checks (warnings only)
say "checking the environment"
if [ -n "$JUDGE_CMD" ] && [ ! -x "$JUDGE_CMD" ]; then
  warn "judge command $JUDGE_CMD is not executable; unlocks will fail with judge_error"
fi

if ! python3 -I - "$OLLAMA_URL" "$WATCH_MODEL" <<'PY'; then
import json, sys, urllib.request
url, model = sys.argv[1].rstrip("/"), sys.argv[2]
try:
    with urllib.request.urlopen(url + "/api/tags", timeout=3) as r:
        names = [m.get("name") for m in json.load(r).get("models", [])]
except Exception as e:
    print(f"Ollama is not reachable at {url} ({e}); focus-watch's vision step will report errors", file=sys.stderr)
    sys.exit(1)
if model not in names and model + ":latest" not in names:
    print(f"Ollama model {model} is not pulled (have: {', '.join(n for n in names if n) or 'none'});"
          f" run: ollama pull {model}", file=sys.stderr)
    sys.exit(1)
print(f"  Ollama ok, {model} present")
PY
  warn "Ollama check failed (see above); install.sh does not manage Ollama"
fi

if [ -f "$ROOT/etc/nsswitch.conf" ] && ! grep -Eq '^hosts:.*\b(resolve|files)\b' "$ROOT/etc/nsswitch.conf"; then
  warn "nsswitch.conf hosts: line has neither resolve nor files; /etc/hosts blocking may not apply"
fi
if grep -Eqs '^[[:space:]]*ReadEtcHosts[[:space:]]*=[[:space:]]*(no|false|0)' \
     "$ROOT/etc/systemd/resolved.conf" "$ROOT"/etc/systemd/resolved.conf.d/*.conf; then
  warn "systemd-resolved has ReadEtcHosts=no; the hosts block will be ignored"
fi
[ -d "$ROOT/etc/chromium/policies/managed" ] \
  || echo "  note: $ROOT/etc/chromium/policies/managed does not exist yet; focusd creates it"

# ---------------------------------------------------------------- services
if [ "$NO_START" -eq 1 ]; then
  say "--no-start: units installed but not enabled or started"
  run systemctl daemon-reload
else
  say "enabling and (re)starting the services (a restart re-locks every group)"
  run systemctl daemon-reload
  run systemctl enable focus-dnslog.service focusd.service
  run systemctl restart focus-dnslog.service focusd.service
  run systemctl --global enable focus-watch.service
  if [ "$REAL" -eq 1 ]; then
    for _ in $(seq 20); do [ -S /run/focusd/focusd.sock ] && break; sleep 0.5; done
    if [ -S /run/focusd/focusd.sock ]; then
      /usr/local/bin/focus status || warn "focus status failed"
    else
      warn "focusd did not create its socket; check: journalctl -u focusd -n 50"
    fi
    systemctl is-active --quiet focus-dnslog.service || warn "focus-dnslog is not active; check: journalctl -u focus-dnslog -n 50"
  fi
fi

say "root part done${ROOT:+ (under $ROOT)}; $WARNINGS warning(s)"
echo "Next, as $CFG_USER (not root):  bash $REPO/install-user.sh"
