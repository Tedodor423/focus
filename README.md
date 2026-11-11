# focus

Theo's anti-procrastination system for this Arch/Omarchy (Hyprland) laptop. It
blocks distracting sites. Unlocking one takes a reason that convinces an AI judge
(`claude -p` on Theo's subscription), within hard limits the judge can't override.
A screen monitor watches window titles (and, when they're unclear, a local vision
model looks at the active window) and records, nudges or penalises procrastination.

The goal is **friction plus accountability**, not perfect security: Theo has root.
Tampering is meant to be visible (the bar warns when the daemon or the monitor
stops), and stopping the daemon fails closed (everything locked).

The build contract is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). This is v1,
**light mode**.

## Status and caveats

- **Built for one machine:** Arch Linux with Omarchy/Hyprland. The bar plugin,
  keybinding and window handling assume that desktop, and paths and comments still
  say `theo`. Elsewhere, expect to adapt it.
- **Friction, not security:** anyone with root (you) can switch it off.
- **The monitor needs a local Ollama vision model** (`[watch] model`, by default
  `qwen3.5:4b`). `install.sh` only checks for it; install and pull it yourself.
  Screenshots go only to that local model.
- **The judge runs `claude -p` on a Claude subscription,** so the Claude Code CLI
  has to be installed and logged in. Your reasons and today's history, including
  window titles the monitor flagged, are sent to Anthropic.

## What gets blocked (light mode)

All of it is in [`etc/config.example.toml`](etc/config.example.toml), commented. In short:

| Group | How | Notes |
|---|---|---|
| YouTube | browser policy only | YouTube Music, embeds and the sign-in hop stay open |
| Reddit | `/etc/hosts` + policy | |
| X | `/etc/hosts` + policy | also the xcancel/nitter mirrors |
| Instagram (feed) | browser policy only | `instagram.com/direct` (DMs) and login stay open |
| Online games | `/etc/hosts` + policy | portals, .io games, chess, GeoGuessr, NYT games |
| Minecraft (online) | `/etc/hosts` + policy | **off** unless enabled |
| Porn | `/etc/hosts` (StevenBlack porn-only list, ~77k names) + policy | not unlockable |

`never_block` in the config lists what must never break (the judge's Anthropic
domains, Google and Microsoft sign-in, your university and file-sync server,
Tailscale, GitHub, package mirrors, ...). The judge's instructions are
[`etc/judge.example.md`](etc/judge.example.md).

## Pieces

| Piece | Runs as | What it does |
|---|---|---|
| `focusd` (`focusd.service`) | root | owns the `/etc/hosts` block and `focus.json` browser policies, budgets, the judge, penalties; socket `/run/focusd/focusd.sock` |
| `focus` | theo | CLI: `status`, `unlock`, `lock`, `log`, `groups`, `appeal`, `watch` |
| `focus-dnslog` (`focus-dnslog.service`) | root | `resolvectl monitor` → `/var/log/focus/dns/YYYY-MM-DD.jsonl`; logs only, kept 30 days |
| `focus-watch` (`focus-watch.service`, user unit) | theo | the screen monitor (`[watch]` in the config; starts in `observe` mode); `--restore` undoes its window actions |
| `theo.focus` bar plugin | theo | lock glyph / countdown, warnings, penalty overlay, **Super+Alt+U** = unlock |

## Setup

The repo tracks examples only. Your real values live in local, git-ignored copies:

```bash
cp etc/config.example.toml etc/config.toml
cp etc/judge.example.md etc/judge.md
```

In `etc/config.toml`, set `user` and the `[judge] command` path, then edit:

- `[safety] never_block`: replace `nextcloud.example.com` with your own file-sync
  server and add your university or employer (sign-in, VPN, library);
- the `[groups.*]`: what gets blocked and what can be unlocked;
- `[watch]`: the monitor's mode, its model, and what counts as work.

Rewrite `etc/judge.md` so it describes you and your work, then install. `install.sh`
uses the local files when they exist; without them it installs the examples and
warns. The commands below use the author's checkout path; substitute yours.

## Install

Nothing is installed by building or testing.

```bash
# 1. root part: package, launchers, config, units; enables + starts focusd and focus-dnslog,
#    globally enables the focus-watch user unit; backs up /etc/hosts once to /var/lib/focus/hosts.orig
pkexec bash /home/theo/nextcloud/code/focus/install.sh          # --no-start: files only

# 2. user part, as theo: bar plugin symlink + enable, Super+Alt+U block in
#    ~/.config/hypr/bindings.lua (backup in ~/.local/state/focus/backups/), start focus-watch
bash /home/theo/nextcloud/code/focus/install-user.sh

# 3. check
focus status
focus watch
journalctl -u focusd -u focus-dnslog -n 50
journalctl --user -u focus-watch -n 50
```

Both installers are idempotent; run them again after changing the repo. Re-running
`install.sh` restarts focusd, which **re-locks every group** (active unlocks end).
`install.sh` checks for Ollama and the `qwen3.5:4b` model but never installs or
changes them; it never touches Omarchy's `color.json`/`omarchy.json` policy files
and never edits `/etc/hosts` itself.

Config changes: edit `etc/config.toml` in the repo and re-run `install.sh` (the
previous `/etc/focus/config.toml` is saved in `/var/lib/focus/backups/`). focusd
and focus-watch pick up the new file on their own.

## Use

```bash
focus                                   # what's locked, active unlocks, budget used
focus unlock                            # pick a group, then explain yourself to the judge
focus unlock youtube -m 15 "the 12-min Seurat tutorial my supervisor sent"
focus lock                              # re-lock everything now
focus appeal                            # "that was work": appeal the latest monitor penalty
focus log --today
focus watch                             # monitor mode, last check, flags, penalties
```

`unlock` and `appeal` take `--pause` (wait for Enter before exiting); the
floating terminal from **Super+Alt+U** and the overlay's *Appeal* button use it.

Limits (config `[limits]`): at most 20 min per grant, 40 min per group per day
(games: 30), 5 min cooldown after a deny, 8 judge calls per hour, 3 follow-up
questions. The day starts at 04:00.

### Screen monitor modes

`[watch] mode` in the config: `off`, `observe` (log only, the default), `nudge`
(notification after 2 procrastinating checks in a row), `enforce` (after 3: a
30-min penalty that blocks the caught domain and hides web-app windows on a special
workspace; appeal with `focus appeal`). Freezing native games (`freeze_classes`)
ships switched off. Start with a few days of `observe` and look at `focus watch` /
`focus log --today` before going further.

`focus-watch --restore` moves every window it hid back and un-freezes anything it
froze, then exits. The unit runs it automatically after every stop or crash
(`ExecStopPost`); run it by hand if a window ever seems stuck on the hidden workspace.

## Uninstall

User part first (it stops focus-watch, which un-freezes and restores any window it
touched), then the root part:

```bash
bash /home/theo/nextcloud/code/focus/uninstall-user.sh            # --purge: also ~/.local/state/focus
pkexec bash /home/theo/nextcloud/code/focus/uninstall.sh          # --purge: also /var/lib/focus, /var/log/focus
```

The root uninstall removes only the focus block from `/etc/hosts` (everything else
byte for byte) and only `focus.json` from the policy dirs. If the block is
malformed it leaves `/etc/hosts` alone and says so; the pre-install copy is
`/var/lib/focus/hosts.orig`.

## Installed layout

```
/usr/local/lib/focus/focus/*.py        package            /usr/local/lib/focus/focus-dnslog
/usr/local/bin/focus, focus-watch      /usr/local/sbin/focusd
/etc/focus/config.toml, judge.md
/etc/systemd/system/focusd.service, focus-dnslog.service
/etc/systemd/user/focus-watch.service
/var/lib/focus/   state.json, events.jsonl, lists/, judge/, hosts.orig, backups/
/var/log/focus/dns/YYYY-MM-DD.jsonl    root:theo 0640
/run/focusd/focusd.sock                root:theo 0660
~/.config/omarchy/plugins/theo.focus -> <repo>/shell/theo.focus
~/.config/hypr/bindings.lua            "-- BEGIN focus" … "-- END focus" block
~/.local/state/focus/                  watch.json, backups/
```

## Browser extension

The blocker can only stop a page from loading, so the installer also forces a small extension into Chromium and Vivaldi. About 5 seconds after a site locks, any tab open on it is replaced with a "locked" page, and that tab goes back to the site when it's unlocked.

Incognito needs one manual step per browser, because no policy can grant it: open `chrome://extensions` (Vivaldi: `vivaldi://extensions`), choose focus guard, then Details, and turn on "Allow in Incognito". Until that's done, incognito refuses to load pages in that browser. `uninstall --purge` deletes the signing key, so the next install gets a new extension id and needs this step again.

## Development

The repo lives on NTFS (`~/nextcloud` → `/mnt/windows`): no exec bits or symlinks
in the repo, so run everything through `python3`/`bash`.

```bash
cd ~/nextcloud/code/focus
python3 -m unittest discover -s tests -v        # all tests, unprivileged; covers the example config and your local one

FOCUS_DEV=1 python3 -I bin/focus status         # launchers import from the repo with FOCUS_DEV=1
FOCUS_ROOT=/tmp/focus-root bash install.sh      # install into a prefix: no root, no systemctl
FOCUS_ROOT=/tmp/focus-root bash uninstall.sh
HOME=/tmp/fakehome FOCUS_SYSTEMCTL=true FOCUS_HYPRCTL=true FOCUS_OMARCHY=true \
  FOCUS_OMARCHY_SHELL=true bash install-user.sh  # user part against a fake HOME
```

`focus-dnslog` can be fed canned monitor output: `focus-dnslog --stdin --outdir DIR`
or `--command "<fake resolvectl>"`.

## Known limits

- `/etc/hosts` matches exact names only; the browser policy covers subdomains
  and paths, but only in Chromium and Vivaldi (no Firefox here).
- YouTube and the Instagram feed are blocked in the browsers only: `mpv`/`yt-dlp`
  can still stream. Instagram is a single-page app, so moving from DMs to the feed
  inside one tab isn't stopped until a reload; the monitor is the backstop.
- The Minecraft game's window class is assumed from GLFW behaviour ("Minecraft 26.3"),
  not observed live.

## License

MIT, see [`LICENSE`](LICENSE).
