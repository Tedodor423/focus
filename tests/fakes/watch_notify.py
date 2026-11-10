"""Fake `notify-send` for focus-watch tests: appends {"argv": [...]} to FAKE_NOTIFY_LOG."""
import json
import os
import sys

with open(os.environ["FAKE_NOTIFY_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
