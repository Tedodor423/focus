#!/bin/bash
# focus: remove the root part (same as install.sh --uninstall). Run the user part
# first, as theo: bash /home/theo/nextcloud/code/focus/uninstall-user.sh
#
#   pkexec bash /home/theo/nextcloud/code/focus/uninstall.sh [--purge] [--force]
#
# Stops and disables focusd/focus-dnslog, removes the focus block from /etc/hosts
# (everything else byte for byte) and focus.json from the policy dirs (Omarchy's
# files untouched), then the installed files and units. Keeps /var/lib/focus
# (events, state, hosts.orig, backups) and /var/log/focus unless --purge.
# --force: continue although focus-watch is still running.
# Test prefix: FOCUS_ROOT=/some/tmp/dir, as for install.sh.
exec bash "$(dirname "${BASH_SOURCE[0]}")/install.sh" --uninstall "$@"
