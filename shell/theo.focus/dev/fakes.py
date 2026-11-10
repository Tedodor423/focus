#!/usr/bin/python3 -I
"""Fake focus / hyprctl / notifier / terminal for dev/harness.sh.

Called as `fakes.py <role> args...` through small wrappers that harness.sh
writes into $FOCUS_DEV_DIR/bin (outside the repo: NTFS has no reliable exec
bits). Nothing here touches the real daemon, real windows or notifications:
every call is appended as a JSON line to $FOCUS_DEV_DIR/<role>.log.

focus:   `status --json` prints the current scenario (scenario names are set
         with `focus --dev-set <name>`); times written as "@+N" / "@-N" are
         seconds relative to $FOCUS_DEV_DIR/base (written once by harness.sh),
         so they stay stable across polls.
hyprctl: `clients -j` prints canned clients; `dispatch <lua>` is only logged.
"""
import json
import os
import sys
import time

DIR = os.environ["FOCUS_DEV_DIR"]


def log(role, argv):
    with open(os.path.join(DIR, role + ".log"), "a") as f:
        f.write(json.dumps(argv) + "\n")


def base():
    with open(os.path.join(DIR, "base")) as f:
        return float(f.read().strip())


def resolve(v, b):
    if isinstance(v, str) and v.startswith("@") and len(v) > 1:
        return b + float(v[1:])
    if isinstance(v, list):
        return [resolve(x, b) for x in v]
    if isinstance(v, dict):
        return {k: resolve(x, b) for k, x in v.items()}
    return v


YT = {"id": "youtube", "label": "YouTube", "enabled": True, "unlockable": True, "locked": True,
      "unlocked_until": None, "used_today_min": 15, "daily_minutes": 40, "max_minutes": 20,
      "cooldown_until": None, "webapp_classes": ["^chrome-(www\\.)?youtube\\.com__"],
      "title_patterns": ["(?i) - YouTube( - [^-]+)?$"], "hosts_count": 0,
      "url_block": ["youtube.com", "youtu.be"], "url_allow": ["music.youtube.com", "youtube.com/embed"]}
RD = {"id": "reddit", "label": "Reddit", "enabled": True, "unlockable": True, "locked": True,
      "unlocked_until": None, "used_today_min": 0, "daily_minutes": 40, "max_minutes": 20,
      "cooldown_until": None, "webapp_classes": ["(?i)^chrome-(www\\.)?reddit\\.com__"],
      "title_patterns": [], "hosts_count": 2, "url_block": ["reddit.com"], "url_allow": []}
PN = {"id": "porn", "label": "Porn", "enabled": True, "unlockable": False, "locked": True,
      "unlocked_until": None, "used_today_min": 0, "daily_minutes": 40, "max_minutes": 20,
      "cooldown_until": None, "webapp_classes": [], "title_patterns": [], "hosts_count": 71234,
      "url_block": [], "url_allow": []}

P1 = {"id": "p1a2b3c4", "since": "@-30", "until": "@+1770", "domain": "news.ycombinator.com",
      "app": "Hacker News", "title": "Hacker News - Vivaldi", "description": "a list of tech news links",
      "source": "title", "reason": "3 procrastinating checks in a row"}
P2 = {"id": "p2b3c4d5", "since": "@+20", "until": "@+1820", "domain": None, "app": "<b>Minecraft</b> & co",
      "title": "Minecraft 1.21.4 - Singleplayer <script>", "description": "a voxel game, survival mode",
      "source": "vision", "reason": "vision verdict 0.93"}
P3 = {"id": "p3c4d5e6", "since": "@+21", "until": "@+1821", "domain": None, "app": "Twitch",
      "title": "somestreamer - Twitch", "description": "", "source": "title", "reason": "distraction pattern"}

P4 = {"id": "p4d5e6f7", "since": "@+30", "until": "@+1830", "domain": None, "app": "Steam",
      "title": "Steam", "description": "", "source": "title", "reason": "distraction pattern"}


def status(groups=None, unlocks=None, penalties=None, last_seen="@-20", mode="observe", health=None):
    groups = groups or [YT, RD, PN]
    extra = {} if health is None else {"health": health}
    return extra | {"ok": True, "version": "0.1.0", "mode": "light", "now": "@+0", "started": "@-5000",
            "day_start": "@-40000", "groups": groups, "unlocks": unlocks or [],
            "lists": {"porn": {"entries": 71234, "updated": "@-90000"}}, "judge_calls_last_hour": 1,
            "penalties": penalties or [],
            "watch": {"mode": mode, "title_interval_seconds": 60, "last_seen": last_seen,
                      "last": None if last_seen is None else {"t": last_seen, "verdict": "working", "app": "foot",
                                                               "title": "nvim", "source": "title"},
                      "flags_today": 3}}


def unlocked(g, until):
    return dict(g, locked=False, unlocked_until=until)


def unlock_rec(g, until):
    return {"group": g["id"], "label": g["label"], "since": "@-600", "until": until, "minutes": 15, "reason": "r"}


SCENARIOS = {
    "locked": (0, status()),
    # YouTube expires 130 s after base: armed now, warned once base+10 s has passed.
    "unlocked": (0, status(groups=[unlocked(YT, "@+130"), unlocked(RD, "@+1500"), PN],
                           unlocks=[unlock_rec(RD, "@+1500"), unlock_rec(YT, "@+130")])),
    "relocked": (0, status(groups=[YT, unlocked(RD, "@+1500"), PN], unlocks=[unlock_rec(RD, "@+1500")])),
    "penalty": (0, status(penalties=[P1])),
    "penalty2": (0, status(penalties=[P1, P2, P3])),
    "ended": (0, status(penalties=[])),
    "penalty4": (0, status(penalties=[P4])),
    "stale-monitor": (0, status(last_seen="@-400")),
    "monitor-off": (0, status(last_seen=None, mode="off")),
    "health": (0, status(health=["hosts: refusing to write (two BEGIN markers)", "judge: not logged in"])),
    "unreachable": (3, {"ok": False, "error": "daemon_unreachable"}),
    "forbidden": (1, {"ok": False, "error": "forbidden", "message": "uid 1000 not allowed"}),
}

CLIENTS = [
    {"address": "0x55aa01", "class": "chrome-www.youtube.com__-Default", "title": "Some video - YouTube", "pid": 1},
    {"address": "0x55aa02", "class": "chrome-youtube.com__-Default", "title": "YouTube", "pid": 1},
    {"address": "0x55aa03", "class": "chrome-music.youtube.com__-Default", "title": "YouTube Music", "pid": 1},
    {"address": "0x55aa04", "class": "chrome-www.reddit.com__-Default", "title": "reddit", "pid": 1},
    {"address": "0x55aa05", "class": "foot", "title": "nvim", "pid": 2},
]


def main():
    role, args = sys.argv[1], sys.argv[2:]
    if role == "focus":
        if args[:1] == ["--dev-set"]:
            with open(os.path.join(DIR, "current"), "w") as f:
                f.write(args[1])
            return 0
        log("focus", args)
        if args == ["status", "--json"]:
            try:
                with open(os.path.join(DIR, "current")) as f:
                    name = f.read().strip()
            except FileNotFoundError:
                name = "locked"
            code, obj = SCENARIOS[name]
            obj = resolve(obj, base())
            if obj.get("ok"):
                obj["now"] = time.time()
            print(json.dumps(obj))
            return code
        if args[:1] == ["lock"]:
            print(json.dumps({"ok": True, "locked": ["youtube", "reddit"]}) if "--json" in args else "Locked: all")
            return 0
        return 0
    if role == "hyprctl":
        log("hyprctl", args)
        if args == ["clients", "-j"]:
            print(json.dumps(CLIENTS))
            return 0
        if args[:1] == ["dispatch"]:
            print("ok")
            return 0
        return 1
    if role in ("notify", "terminal"):
        log(role, args)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
