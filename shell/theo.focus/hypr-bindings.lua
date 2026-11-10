-- BEGIN focus
-- focus blocker (~/nextcloud/code/focus, bar plugin theo.focus): floating
-- terminal running `focus unlock --pause`. Goes through the bar plugin, which
-- refreshes the bar when the terminal is done; when the shell or the plugin
-- isn't running, opens the same floating terminal directly.
o.bind("SUPER + ALT + U", "Focus unlock", "omarchy-shell theo.focus unlock || setsid uwsm-app -- xdg-terminal-exec --app-id=org.omarchy.terminal --title=Focus -e /usr/local/bin/focus unlock --pause")
-- END focus
