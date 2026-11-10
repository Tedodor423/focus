"""Fake `claude -p` for focusd tests (run via python3, never on PATH).

judge.command in the test config is [python3, <this file>, SCRIPT]. SCRIPT is
a JSON file {"responses": [step, ...]}; call N uses step N (the last step
repeats). A step is one of:
  {"decision": "grant", "minutes": 15, "message": "ok"}  normal verdict
  {"raw": "<stdout text>"}                              print this verbatim
  {"is_error": true}                                    is_error output
  {"exit": 3, "stdout": "..."}                         print stdout (optional), exit 3
  {"sleep": 5, ...}                                     sleep first (spawns a
        child `sleep` and writes its pid to SCRIPT.child, to check that the
        daemon kills the whole process group on timeout)
  {"sleep": 5, "ignore_term": true}                     ... and ignore SIGTERM
        (fake and child), so only the daemon's follow-up SIGKILL ends them
Every call appends {"argv", "stdin", "env", "cwd"} to SCRIPT.calls.jsonl.
"""

import json
import os
import signal
import subprocess
import sys
import time


def main():
    script = sys.argv[1]
    stdin = sys.stdin.read()
    calls_path = script + ".calls.jsonl"
    try:
        with open(calls_path) as f:
            n = sum(1 for _ in f)
    except FileNotFoundError:
        n = 0
    with open(calls_path, "a") as f:
        f.write(json.dumps({"argv": sys.argv[2:], "stdin": stdin, "env": dict(os.environ),
                            "cwd": os.getcwd()}) + "\n")
    with open(script) as f:
        steps = json.load(f)["responses"]
    step = steps[min(n, len(steps) - 1)]
    if step.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # inherited by the child
    if "sleep" in step:
        child = subprocess.Popen(["sleep", str(step["sleep"] * 4)])
        with open(script + ".child", "w") as f:
            f.write(str(child.pid))
        time.sleep(step["sleep"])
    if "exit" in step:
        sys.stdout.write(step.get("stdout", ""))
        print("fake failure", file=sys.stderr)
        sys.exit(step["exit"])
    if "raw" in step:
        sys.stdout.write(step["raw"])
        return
    if step.get("is_error"):
        print(json.dumps({"type": "result", "is_error": True, "result": "fake error"}))
        return
    out = {k: step[k] for k in ("decision", "minutes", "message") if k in step}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "", "structured_output": out}))


if __name__ == "__main__":
    main()
