#!/bin/bash
# focus: per-user part of the install. Run as theo (never root), after install.sh:
#
#   bash /home/theo/nextcloud/code/focus/install-user.sh [--no-session]
#   bash /home/theo/nextcloud/code/focus/install-user.sh --uninstall [--purge]
#                                       (uninstall-user.sh does the same)
#
# Install (idempotent):
#   1. ~/.config/omarchy/plugins/theo.focus -> <repo>/shell/theo.focus (symlink),
#      omarchy-shell shell rescanPlugins,
#      omarchy plugin enable theo.focus --section centerEnd --after theo.recall
#   2. the `-- BEGIN focus` … `-- END focus` block from shell/theo.focus/hypr-bindings.lua
#      inserted into (or replaced in place in) ~/.config/hypr/bindings.lua, then
#      hyprctl reload + hyprctl configerrors (errors = failure). A copy of the old
#      bindings.lua goes to ~/.local/state/focus/backups/ whenever it changes.
#   3. the focus-watch user unit (installed by install.sh in /etc/systemd/user):
#      systemctl --user daemon-reload, enable, restart.
# Uninstall: stop + disable the unit (focus-watch restores hidden/frozen windows on
# stop), remove the bindings block, omarchy plugin disable, remove the symlink.
# --purge also deletes ~/.local/state/focus (watch.json, backups).
#
# --no-session  only touch files (symlink, bindings.lua); run no omarchy/hyprctl/systemctl.
#
# Tests: run with a fake HOME (and XDG_STATE_HOME); the session commands can be pointed
# at fakes with FOCUS_SYSTEMCTL, FOCUS_HYPRCTL, FOCUS_OMARCHY, FOCUS_OMARCHY_SHELL;
# FOCUS_ROOT prefixes the /etc/systemd/user path that is checked.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ACTION=install SESSION=1 PURGE=0
for arg in "$@"; do
  case $arg in
    --uninstall) ACTION=uninstall ;;
    --no-session) SESSION=0 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "focus install-user: unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; FAILED=$((FAILED + 1)); }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
FAILED=0

[ "$(id -u)" -ne 0 ] || die "run this as your user, not root (the root part is install.sh)"
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "HOME is not set or not a directory"

PLUGIN_ID=theo.focus
PLUGIN_SRC=$REPO/shell/$PLUGIN_ID
PLUGIN_LINK=$HOME/.config/omarchy/plugins/$PLUGIN_ID
BLOCK_SRC=$PLUGIN_SRC/hypr-bindings.lua
BINDINGS=$HOME/.config/hypr/bindings.lua
STATE=${XDG_STATE_HOME:-$HOME/.local/state}/focus
BACKUPS=$STATE/backups
UNIT=focus-watch.service
UNIT_FILE=${FOCUS_ROOT:-}/etc/systemd/user/$UNIT

SYSTEMCTL=${FOCUS_SYSTEMCTL:-systemctl}
HYPRCTL=${FOCUS_HYPRCTL:-hyprctl}
OMARCHY=${FOCUS_OMARCHY:-omarchy}
OMARCHY_SHELL=${FOCUS_OMARCHY_SHELL:-omarchy-shell}

# Edit the marked block in bindings.lua. Modes: install <file> <block-src> <backup-dir>,
# remove <file> <backup-dir>. Exit 0 = done/unchanged, 3 = bad block source,
# 4 = malformed block in bindings.lua (left untouched), 5 = Lua syntax error in the block.
bindings_edit() {
  python3 -I - "$@" <<'PY'
import os, re, shutil, subprocess, sys, tempfile, time

BEGIN, END = "-- BEGIN focus", "-- END focus"
mode, path = sys.argv[1], sys.argv[2]

def markers(lines):
    b = [i for i, l in enumerate(lines) if l.strip() == BEGIN]
    e = [i for i, l in enumerate(lines) if l.strip() == END]
    return b, e

def write(path, text, old, backup_dir):
    real = os.path.realpath(path)  # keep a symlinked bindings.lua a symlink
    if old is not None:
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        bak = os.path.join(backup_dir, "bindings.lua." + time.strftime("%Y%m%d%H%M%S"))
        n = 1
        while os.path.exists(bak + ("" if n == 1 else f".{n}")):
            n += 1
        bak += "" if n == 1 else f".{n}"
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old)
        print(f"  backup: {bak}")
    d = os.path.dirname(real)
    os.makedirs(d, exist_ok=True)
    mode_bits = os.stat(real).st_mode & 0o7777 if os.path.exists(real) else 0o644
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bindings.lua.focus.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, mode_bits)
        os.replace(tmp, real)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None

if mode == "install":
    src, backup_dir = sys.argv[3], sys.argv[4]
    s_lines = (read(src) or "").splitlines()
    b, e = markers(s_lines)
    if len(b) != 1 or len(e) != 1 or e[0] < b[0]:
        print(f"  {src}: needs exactly one '{BEGIN}' line followed by one '{END}' line", file=sys.stderr)
        sys.exit(3)
    block = [l.rstrip() for l in s_lines[b[0]:e[0] + 1]]
    luac = shutil.which("luac")
    if luac:
        r = subprocess.run([luac, "-p", "-"], input="\n".join(block) + "\n", text=True,
                           capture_output=True)
        if r.returncode != 0:
            print(f"  Lua syntax error in the block: {r.stderr.strip()}", file=sys.stderr)
            sys.exit(5)
    old = read(path)
    text = old or ""
    lines = text.splitlines(keepends=True)
    b, e = markers(lines)
    if b or e:
        if len(b) != 1 or len(e) != 1 or e[0] < b[0]:
            print(f"  {path}: malformed focus block ({len(b)} BEGIN, {len(e)} END); left untouched,"
                  " fix it by hand", file=sys.stderr)
            sys.exit(4)
        new = "".join(lines[:b[0]]) + "\n".join(block) + "\n" + "".join(lines[e[0] + 1:])
        where = "replaced in place"
    else:
        new = text
        if new and not new.endswith("\n"):
            new += "\n"
        if new and not new.endswith("\n\n"):
            new += "\n"
        new += "\n".join(block) + "\n"
        where = "appended"
    # Warn about the same key combo bound outside our block.
    combos = re.findall(r'\bbind\w*\(\s*"([^"]+)"', "\n".join(block))
    nl = new.splitlines(keepends=True)
    nb, ne = markers(nl)
    outside = "".join(nl[:nb[0]] + nl[ne[0] + 1:])
    norm = lambda k: re.sub(r"\s+", "", k).upper()
    for c in combos:
        for m in re.finditer(r'^\s*[^-\n]*\bbind\w*\(\s*"([^"]+)"', outside, re.M):
            if norm(m.group(1)) == norm(c):
                print(f"  warning: {c} is also bound outside the focus block in {path}", file=sys.stderr)
    if new == (old or ""):
        print(f"  {path}: focus block already up to date")
        sys.exit(0)
    write(path, new, old, backup_dir)
    print(f"  {path}: focus block {where}")

elif mode == "remove":
    backup_dir = sys.argv[3]
    old = read(path)
    if old is None:
        print(f"  {path} missing, nothing to remove"); sys.exit(0)
    lines = old.splitlines(keepends=True)
    b, e = markers(lines)
    if not b and not e:
        print(f"  {path}: no focus block"); sys.exit(0)
    if len(b) != 1 or len(e) != 1 or e[0] < b[0]:
        print(f"  {path}: malformed focus block ({len(b)} BEGIN, {len(e)} END); left untouched,"
              " remove it by hand", file=sys.stderr)
        sys.exit(4)
    head, tail = "".join(lines[:b[0]]), "".join(lines[e[0] + 1:])
    if not tail and head.endswith("\n\n"):
        head = head[:-1]  # the blank separator line install added
    write(path, head + tail, old, backup_dir)
    print(f"  {path}: focus block removed")
else:
    sys.exit(f"unknown mode {mode}")
PY
}

hypr_reload_check() {
  [ "$SESSION" -eq 1 ] || return 0
  if ! "$HYPRCTL" reload >/dev/null 2>&1; then
    fail "hyprctl reload failed (is Hyprland running?)"
    return 0
  fi
  local out errs
  out=$("$HYPRCTL" configerrors -j 2>/dev/null || true)
  errs=$(printf '%s' "$out" | python3 -I -c '
import json, sys
raw = sys.stdin.read()
try:
    v = json.loads(raw)
    errs = [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
except ValueError:
    errs = [l for l in raw.splitlines() if l.strip() and "no errors" not in l.lower()]
print("\n".join(errs))')
  if [ -n "$errs" ]; then
    fail "Hyprland reports config errors after the reload:"
    printf '%s\n' "$errs" | sed 's/^/    /' >&2
  else
    echo "  hyprctl configerrors: none"
  fi
}

# ---------------------------------------------------------------- uninstall
if [ "$ACTION" = uninstall ]; then
  if [ "$SESSION" -eq 1 ]; then
    say "stopping the screen monitor (it restores hidden/frozen windows on stop)"
    "$SYSTEMCTL" --user disable --now "$UNIT" 2>&1 | sed 's/^/  /' || true
  fi

  say "removing the focus block from $BINDINGS"
  rc=0; bindings_edit remove "$BINDINGS" "$BACKUPS" || rc=$?
  [ "$rc" -eq 0 ] || fail "bindings.lua not changed (exit $rc)"
  [ "$rc" -eq 0 ] && hypr_reload_check

  say "removing the bar plugin"
  if [ "$SESSION" -eq 1 ]; then
    "$OMARCHY" plugin disable "$PLUGIN_ID" 2>&1 | sed 's/^/  /' || true
  fi
  if [ -L "$PLUGIN_LINK" ]; then
    rm -f -- "$PLUGIN_LINK"; echo "  removed $PLUGIN_LINK"
  elif [ -e "$PLUGIN_LINK" ]; then
    fail "$PLUGIN_LINK is not a symlink; left in place"
  fi
  if [ "$SESSION" -eq 1 ]; then
    "$OMARCHY_SHELL" shell rescanPlugins >/dev/null 2>&1 || true
  fi

  if [ "$PURGE" -eq 1 ]; then
    rm -rf -- "${STATE:?}"; echo "  removed $STATE"
  fi
  [ -f "$UNIT_FILE" ] && echo "  note: $UNIT_FILE stays globally enabled until the root uninstall (uninstall.sh)"
  if [ "$FAILED" -eq 0 ]; then say "user part uninstalled"; exit 0; fi
  say "user part uninstalled with $FAILED error(s)"; exit 1
fi

# ---------------------------------------------------------------- install
say "checking the repo ($REPO)"
[ -d "$PLUGIN_SRC" ] || die "missing $PLUGIN_SRC (the bar plugin)"
[ -f "$PLUGIN_SRC/manifest.json" ] || die "missing $PLUGIN_SRC/manifest.json"
[ -f "$BLOCK_SRC" ] || die "missing $BLOCK_SRC (the keybinding block)"
command -v python3 >/dev/null || die "python3 is required"

say "bar plugin: $PLUGIN_LINK -> $PLUGIN_SRC"
mkdir -p "$(dirname "$PLUGIN_LINK")"
if [ -L "$PLUGIN_LINK" ]; then
  if [ "$(readlink "$PLUGIN_LINK")" = "$PLUGIN_SRC" ]; then
    echo "  symlink already in place"
  else
    echo "  re-pointing the symlink (was $(readlink "$PLUGIN_LINK"))"
    ln -sfn -- "$PLUGIN_SRC" "$PLUGIN_LINK"
  fi
elif [ -e "$PLUGIN_LINK" ]; then
  die "$PLUGIN_LINK exists and is not a symlink; refusing to replace it"
else
  ln -s -- "$PLUGIN_SRC" "$PLUGIN_LINK"
  echo "  created"
fi
if [ "$SESSION" -eq 1 ]; then
  "$OMARCHY_SHELL" shell rescanPlugins >/dev/null 2>&1 || warn "omarchy-shell shell rescanPlugins failed (is the shell running?)"
  if "$OMARCHY" plugin enable "$PLUGIN_ID" --section centerEnd --after theo.recall 2>&1 | sed 's/^/  /'; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    :
  elif "$OMARCHY" plugin enable "$PLUGIN_ID" --section centerEnd 2>&1 | sed 's/^/  /'; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    warn "enabled $PLUGIN_ID in centerEnd, but not after theo.recall"
  else
    fail "omarchy plugin enable $PLUGIN_ID failed: the bar item is not active (shell.json doesn't list it)"
  fi
fi

say "keybinding block in $BINDINGS"
rc=0; bindings_edit install "$BINDINGS" "$BLOCK_SRC" "$BACKUPS" || rc=$?
case $rc in
  0) hypr_reload_check ;;
  3) fail "bad block source $BLOCK_SRC; bindings.lua not changed" ;;
  4) fail "bindings.lua has a malformed focus block; not changed" ;;
  5) fail "the focus block has a Lua syntax error; bindings.lua not changed" ;;
  *) fail "editing bindings.lua failed (exit $rc)" ;;
esac

say "screen monitor ($UNIT)"
if [ ! -f "$UNIT_FILE" ]; then
  warn "$UNIT_FILE not found: run the root part first (pkexec bash $REPO/install.sh); monitor not enabled"
elif [ "$SESSION" -eq 1 ]; then
  "$SYSTEMCTL" --user daemon-reload || fail "systemctl --user daemon-reload failed"
  "$SYSTEMCTL" --user enable "$UNIT" 2>&1 | sed 's/^/  /' || true
  if "$SYSTEMCTL" --user restart "$UNIT"; then
    echo "  started (logs: journalctl --user -u $UNIT -f)"
  else
    fail "could not start $UNIT; check: journalctl --user -u $UNIT -n 50"
  fi
else
  echo "  --no-session: not enabling $UNIT"
fi

if [ "$FAILED" -eq 0 ]; then
  say "user part done. Super+Alt+U: unlock · focus status · focus watch"
  exit 0
fi
say "user part finished with $FAILED error(s) (see above)"
exit 1
