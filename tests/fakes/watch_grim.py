"""Fake `grim` for focus-watch tests: writes a solid-colour PPM to the output path.

FAKE_GRIM_LOG   append {"argv", "path", "mode"} (mode = permission bits of the file before writing)
FAKE_GRIM_SIZE  "WxH" of the image to write (default 2000x1200)
FAKE_GRIM_FAIL  if set: write a partial file, then exit 1
"""
import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    path = argv[-1]
    try:
        mode = oct(os.stat(path).st_mode & 0o777)
    except OSError:
        mode = None
    with open(os.environ["FAKE_GRIM_LOG"], "a") as f:
        f.write(json.dumps({"argv": argv, "path": path, "mode": mode}) + "\n")
    w, h = (int(v) for v in os.environ.get("FAKE_GRIM_SIZE", "2000x1200").split("x"))
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        if os.environ.get("FAKE_GRIM_FAIL"):
            f.write(b"\x10\x20")
            return 1
        f.write(bytes([40, 90, 160]) * (w * h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
