#!/bin/bash
# focus: remove the per-user part (same as install-user.sh --uninstall). Run as theo,
# before the root uninstall:
#
#   bash /home/theo/nextcloud/code/focus/uninstall-user.sh [--purge] [--no-session]
#
# Stops and disables the focus-watch user unit (it un-freezes and restores every
# window it touched), removes the `-- BEGIN focus` block from ~/.config/hypr/bindings.lua
# (backup in ~/.local/state/focus/backups/), runs `omarchy plugin disable theo.focus`
# and removes the plugin symlink. --purge also deletes ~/.local/state/focus.
exec bash "$(dirname "${BASH_SOURCE[0]}")/install-user.sh" --uninstall "$@"
