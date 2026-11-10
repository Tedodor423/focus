#!/bin/bash
# Offscreen end-to-end check of the theo.focus plugin, without the real
# daemon, windows or notifications:
#   bash shell/theo.focus/dev/harness.sh
# Builds a throwaway Quickshell config in $FOCUS_DEV_DIR (default
# $XDG_RUNTIME_DIR/theo-focus-dev) with qs.Commons / qs.Ui linked from
# /usr/share/omarchy/shell and the plugin linked as `plugin`, writes wrappers
# for dev/fakes.py (focus, hyprctl, notifier, terminal), runs dev/Harness.qml
# under `qs` with QT_QPA_PLATFORM=offscreen (no window on screen) and leaves
# PNGs in $FOCUS_DEV_DIR/shots. Exit status 0 = all checks passed.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
plugin=$(dirname "$here")
dir="${FOCUS_DEV_DIR:-${XDG_RUNTIME_DIR:-/tmp}/theo-focus-dev}"

rm -rf "$dir"
mkdir -p "$dir/qsroot" "$dir/bin" "$dir/shots"
ln -sfn /usr/share/omarchy/shell/Commons "$dir/qsroot/Commons"
ln -sfn /usr/share/omarchy/shell/Ui "$dir/qsroot/Ui"
ln -sfn "$plugin" "$dir/qsroot/plugin"
for role in focus hyprctl notify terminal; do
  printf '#!/bin/sh\nexec /usr/bin/python3 -I %q %s "$@"\n' "$here/fakes.py" "$role" > "$dir/bin/$role"
  chmod 755 "$dir/bin/$role"
done
python3 -c 'import time; print(time.time())' > "$dir/base"
echo locked > "$dir/current"

cat > "$dir/qsroot/shell.qml" <<'QML'
//@ pragma OmarchyLoadPatch
import QtQuick
import Quickshell
import "plugin/dev" as D

ShellRoot {
  FloatingWindow {
    implicitWidth: 960
    implicitHeight: 700
    color: "black"
    D.Harness { anchors.fill: parent }
  }
}
QML

export FOCUS_DEV_DIR="$dir"
export FOCUS_BIN="$dir/bin/focus" FOCUS_HYPRCTL="$dir/bin/hyprctl"
export FOCUS_PLUGIN_NOTIFY="$dir/bin/notify" FOCUS_PLUGIN_TERMINAL="$dir/bin/terminal"
export FOCUS_PLUGIN_POLL_MS=600000 FOCUS_PLUGIN_STATE="$dir/shown.json"
set +e
QT_QPA_PLATFORM=offscreen QML_XHR_ALLOW_FILE_READ=1 QT_FORCE_STDERR_LOGGING=1 \
  timeout 120 qs -p "$dir/qsroot" 2>&1 \
  | sed -e 's/\x1b\[[0-9;]*m//g' \
  | grep -v -E 'WAYLAND_DISPLAY|--- WARNING|actually running wayland|window masks|Launching config|Shell ID|Saving logs|Configuration Loaded|most functionality'
status=${PIPESTATUS[0]}
ls "$dir/shots"
exit "$status"
