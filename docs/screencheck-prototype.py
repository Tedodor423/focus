#!/usr/bin/env python3
"""Prototype: ask a local Ollama vision model what is on a screenshot.

usage: screencheck.py MODEL IMAGE [IMAGE...]
Prints one JSON line per image with the verdict and timings.
"""
import base64, json, subprocess, sys, tempfile, time, urllib.request, os

OLLAMA = "http://127.0.0.1:11434/api/chat"
MAX_W = int(os.environ.get("MAX_W", "1440"))

SYSTEM = """You look at a screenshot of Theo's laptop screen and decide whether he is working or procrastinating.
Work: research, scientific papers and PDFs, code, terminals, writing, documents, spreadsheets, study material, work email or work chat, documentation.
Procrastination: social media feeds (Reddit, X, Instagram, Facebook, TikTok), YouTube or other video entertainment, games, shopping, news browsing, memes, porn.
Unclear: lock screen, empty desktop, music player alone, settings, anything you cannot judge.
Judge the main, largest focused content. Read visible text (titles, URLs, headings) to decide."""

SCHEMA = {
    "type": "object",
    "properties": {
        "app": {"type": "string", "description": "the app or website in focus"},
        "description": {"type": "string", "description": "one sentence: what is on screen"},
        "verdict": {"type": "string", "enum": ["working", "procrastinating", "unclear"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["app", "description", "verdict", "confidence"],
}


def shrink(path):
    out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    subprocess.run(["magick", path, "-resize", f"{MAX_W}x>", "-quality", "85", out], check=True)
    return out


def ask(model, image):
    small = shrink(image)
    try:
        b64 = base64.b64encode(open(small, "rb").read()).decode()
    finally:
        os.unlink(small)
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "num_ctx": 4096},
        "keep_alive": "10m",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "What is on this screen? Reply with the JSON verdict.", "images": [b64]},
        ],
    }
    req = urllib.request.Request(OLLAMA, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.load(r)
    wall = time.time() - t
    try:
        verdict = json.loads(resp["message"]["content"])
    except Exception:
        verdict = {"raw": resp.get("message", {}).get("content")}
    ns = 1e9
    return {
        "image": os.path.basename(image),
        **verdict,
        "wall_s": round(wall, 1),
        "load_s": round(resp.get("load_duration", 0) / ns, 1),
        "prompt_tok": resp.get("prompt_eval_count"),
        "prompt_s": round(resp.get("prompt_eval_duration", 0) / ns, 1),
        "gen_tok": resp.get("eval_count"),
        "gen_s": round(resp.get("eval_duration", 0) / ns, 1),
    }


if __name__ == "__main__":
    model, images = sys.argv[1], sys.argv[2:]
    for img in images:
        try:
            print(json.dumps(ask(model, img), ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({"image": os.path.basename(img), "error": str(e)}), flush=True)
