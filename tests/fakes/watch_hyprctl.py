"""Fake `hyprctl` for focus-watch tests (run as `python3 watch_hyprctl.py ARGS`).

Scenario: JSON file named by FAKE_HYPR_STATE:
  {"activewindow": {...} | {}, "clients": [...], "monitors": [...],
   "dispatch_rc": 0, "dispatch_out": "ok", "fail": ["clients"]}
Every invocation is appended to FAKE_HYPR_LOG as one JSON line {"argv": [...]}.
`dispatch hl.dsp.window.move({...})` updates the client's workspace in the scenario,
so tests can check where a window ended up.
"""
import json
import os
import re
import sys

MOVE_RE = re.compile(
    r'^hl\.dsp\.window\.move\(\{ workspace = "((?:[^"\\]|\\.)*)", follow = false, '
    r'window = "address:(0x[0-9a-fA-F]+)" \}\)$'
)


def main() -> int:
    argv = sys.argv[1:]
    state_path = os.environ["FAKE_HYPR_STATE"]
    with open(os.environ["FAKE_HYPR_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv}) + "\n")
    with open(state_path) as f:
        state = json.load(f)
    if not argv:
        return 1
    cmd = argv[0]
    if cmd in state.get("fail", []):
        print("error: fake failure", file=sys.stderr)
        return 3
    if cmd in ("activewindow", "activeworkspace", "clients", "monitors") and argv[1:] == ["-j"]:
        default = {"activewindow": {}, "activeworkspace": {"id": 1, "name": "1"}, "clients": [],
                   "monitors": [{"name": "eDP-1", "solitaryBlockedBy": ["WINDOWED"]}]}[cmd]
        print(json.dumps(state.get(cmd, default)))
        return 0
    if cmd == "dispatch" and len(argv) == 2:
        rc = state.get("dispatch_rc", 0)
        print(state.get("dispatch_out", "ok"))
        m = MOVE_RE.match(argv[1])
        if m and rc == 0:
            ws, addr = m.group(1), m.group(2)
            for c in state.get("clients", []):
                if c.get("address") == addr:
                    if ws.startswith("special:"):
                        c["workspace"] = {"id": -98, "name": ws}
                    elif ws.isdigit():
                        c["workspace"] = {"id": int(ws), "name": ws}
                    else:
                        c["workspace"] = {"id": -1, "name": ws.removeprefix("name:")}
            tmp = state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, state_path)
        return rc
    print(f"fake hyprctl: unsupported {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
