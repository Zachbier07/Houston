# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai", "opencv-python", "python-dotenv"]
# ///
"""
RUNS ON: laptop (calls the Gemini API in the cloud). Throwaway bake-off tool, not part of app/.

Sends every labeled photo to the brain model twice and scores where it points.

Input:  data/bakeoff/photos/*.jpg + data/bakeoff/labels.json (from tools/label.py)
Output: data/bakeoff/results.csv
        data/bakeoff/overlays/<model>/<photo>_<run>.jpg
        a summary table + a list of photos where the two runs disagree

Needs GEMINI_API_KEY in .env at the repo root.

Usage:
  uv run tools/bakeoff.py
  uv run tools/bakeoff.py --thinking medium     # compare against the docs' recommended level

Point format (from Google's Gemini Robotics ER docs): [y, x], normalized 0-1000.
Normalized means it doesn't depend on the image size we send, so we convert
straight to full-size pixels: x_px = x / 1000 * full_width, y_px = y / 1000 * full_height.
"""
import argparse
import base64
import concurrent.futures
import csv
import json
import os
import statistics
import time
from pathlib import Path

import cv2
from dotenv import load_dotenv
from google import genai

ROOT = Path(__file__).resolve().parent.parent
BAKEOFF = ROOT / "data" / "bakeoff"
PHOTOS = BAKEOFF / "photos"
LABELS = BAKEOFF / "labels.json"
RESULTS = BAKEOFF / "results.csv"
OVERLAYS = BAKEOFF / "overlays"

MODELS = ["gemini-robotics-er-2-preview"]
RUNS = 2
SEND_WIDTH = 640
TIMEOUT_S = 10

SYSTEM_PROMPT = """You are Houston, a desk robot that answers by pointing a laser at things on a desk.
You get one photo of the desk from an overhead camera and what the user said.

Reply with JSON only, no other text:
{"action": "find" | "guide" | "check" | "speak",
 "point": [y, x] or null,
 "say": "one short spoken line"}

- Points are [y, x], normalized 0-1000 (0,0 = top-left of the image).
- find: "point" is the object they asked for. Use null if it isn't visible.
- check: "point" is the mistake in their build. "say" explains it in one line.
- guide: add "steps": [{"point": [y, x], "say": "..."}, ...] in order. "point" = the first step's point.
- speak: anything that doesn't need pointing. "point" is null.
- Keep "say" under 15 words. It is read aloud."""

POINT_SCHEMA = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["find", "guide", "check", "speak"]},
        "point": POINT_SCHEMA,  # leave it out when there's nothing to point at
        "say": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"point": POINT_SCHEMA, "say": {"type": "string"}},
                "required": ["point", "say"],
            },
        },
    },
    "required": ["action", "say"],
}


# ---------- calling the model ----------

def shrink_jpeg(image):
    """Resize to SEND_WIDTH px wide and return base64 JPEG."""
    h, w = image.shape[:2]
    small = cv2.resize(image, (SEND_WIDTH, round(h * SEND_WIDTH / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode("utf-8")


def call_gemini(client, model, image_b64, question, thinking):
    interaction = client.interactions.create(
        model=model,
        input=[
            {"type": "image", "data": image_b64, "mime_type": "image/jpeg"},
            {"type": "text", "text": f"{SYSTEM_PROMPT}\n\nUser said: {question}"},
        ],
        generation_config={"thinking_level": thinking},
        response_format={"type": "text", "mime_type": "application/json", "schema": RESPONSE_SCHEMA},
    )
    return interaction.output_text


def call_with_timeout(pool, fn, *args):
    """Run one API call with a hard TIMEOUT_S wall-clock limit."""
    future = pool.submit(fn, *args)
    return future.result(timeout=TIMEOUT_S)


# ---------- scoring ----------

def parse_response(raw):
    """Return the parsed dict, or None if it isn't valid JSON with action + say."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "action" not in data or "say" not in data:
        return None
    return data


def to_pixels(point, full_w, full_h):
    """[y, x] in 0-1000 -> (x_px, y_px) in the full-size photo. None if malformed."""
    if not (isinstance(point, list) and len(point) == 2):
        return None
    y, x = point
    if not all(isinstance(v, (int, float)) for v in (x, y)):
        return None
    return round(x / 1000 * full_w), round(y / 1000 * full_h)


def target_point(data):
    """The point we'd aim the laser at: steps[0] for guide, else point."""
    if data.get("action") == "guide" and data.get("steps"):
        return data["steps"][0].get("point")
    return data.get("point")


def is_hit(label, pixel):
    box = label["box"]
    if box is None:  # speak: a hit means it didn't point at anything
        return pixel is None
    if pixel is None:
        return False
    x1, y1, x2, y2 = box
    return x1 <= pixel[0] <= x2 and y1 <= pixel[1] <= y2


# ---------- output ----------

def draw_overlay(image, label, data, pixel, raw, error, path):
    out = image.copy()
    if label["box"]:
        x1, y1, x2, y2 = label["box"]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 3)
    if data and data.get("action") == "guide":  # small numbered dots for every guide step
        h, w = out.shape[:2]
        for i, step in enumerate(data.get("steps") or [], start=1):
            p = to_pixels(step.get("point"), w, h)
            if p:
                cv2.circle(out, p, 6, (0, 0, 255), -1)
                cv2.putText(out, str(i), (p[0] + 8, p[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    if pixel:
        cv2.circle(out, pixel, 12, (0, 0, 255), -1)
        cv2.circle(out, pixel, 14, (255, 255, 255), 2)

    text = f"Q: {label['question']}\n" + (f"ERROR: {error}" if error else (raw or ""))
    lines = []
    for paragraph in text.splitlines():
        lines += [paragraph[i:i + 70] for i in range(0, max(len(paragraph), 1), 70)]
    lines = lines[:10]
    cv2.rectangle(out, (0, 0), (min(out.shape[1], 900), 12 + 22 * len(lines)), (0, 0, 0), -1)
    for i, line in enumerate(lines):
        cv2.putText(out, line, (8, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), out)


def print_summary(rows):
    print("\n" + "=" * 78)
    print(f"{'model':32s} {'hits':>7s} {'action':>7s} {'json':>7s} {'median s':>9s} {'max s':>7s}")
    for model in MODELS:
        mine = [r for r in rows if r["model"] == model]
        n = len(mine)
        if n == 0:
            continue
        latencies = [r["latency_s"] for r in mine]
        print(f"{model:32s} "
              f"{sum(r['hit'] for r in mine):>3d}/{n:<3d} "
              f"{sum(r['action_correct'] for r in mine):>3d}/{n:<3d} "
              f"{sum(r['json_valid'] for r in mine):>3d}/{n:<3d} "
              f"{statistics.median(latencies):>9.2f} {max(latencies):>7.2f}")
    print("Pass bar (stack doc): 13/15 hits per run, every JSON valid + right action, median under 2.5 s.")

    print("\nRuns that disagree (same model, same photo):")
    flagged = 0
    for model in MODELS:
        photos = sorted({r["photo"] for r in rows if r["model"] == model})
        for photo in photos:
            runs = [r for r in rows if r["model"] == model and r["photo"] == photo]
            if len({(r["hit"], r["action"]) for r in runs}) > 1:
                flagged += 1
                detail = ", ".join(f"run {r['run']}: {'hit' if r['hit'] else 'miss'}/{r['action'] or '-'}"
                                   for r in runs)
                print(f"  {model}  {photo}  ({detail})")
    if not flagged:
        print("  none")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thinking", default="minimal", choices=["minimal", "low", "medium", "high"],
                        help="Gemini thinking_level (minimal is the lowest the robotics model supports)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is missing. Put it in .env at the repo root.")
    client = genai.Client(api_key=api_key)

    if not LABELS.exists():
        raise SystemExit(f"No {LABELS}. Run tools/label.py first.")
    labels = json.loads(LABELS.read_text())
    print(f"{len(labels)} photos x {len(MODELS)} model(s) x {RUNS} runs, thinking={args.thinking}")

    rows = []
    # max_workers > 1 so a hung call that timed out doesn't block the next one
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for model in MODELS:
            for label in labels:
                image = cv2.imread(str(PHOTOS / label["photo"]))
                if image is None:
                    print(f"  can't read {label['photo']}, skipping")
                    continue
                full_h, full_w = image.shape[:2]
                image_b64 = shrink_jpeg(image)

                for run in range(1, RUNS + 1):
                    raw, error = None, ""
                    start = time.perf_counter()
                    try:
                        raw = call_with_timeout(pool, call_gemini, client, model, image_b64,
                                                label["question"], args.thinking)
                    except concurrent.futures.TimeoutError:
                        error = f"timeout after {TIMEOUT_S} s"
                    except Exception as e:  # any API error counts as a miss; keep going
                        error = f"{type(e).__name__}: {e}"
                    latency = time.perf_counter() - start

                    data = parse_response(raw) if raw is not None else None
                    pixel = to_pixels(target_point(data), full_w, full_h) if data else None
                    hit = bool(data) and is_hit(label, pixel)
                    action = data.get("action") if data else ""

                    rows.append({
                        "model": model,
                        "photo": label["photo"],
                        "run": run,
                        "hit": hit,
                        "action_correct": action == label["action"],
                        "json_valid": data is not None,
                        "latency_s": round(latency, 3),
                        "action": action,
                        "error": error,
                        "raw": raw if raw is not None else "",
                    })
                    stem = Path(label["photo"]).stem
                    draw_overlay(image, label, data, pixel, raw, error,
                                 OVERLAYS / model / f"{stem}_{run}.jpg")
                    status = error or ("HIT" if hit else "miss")
                    print(f"  {model}  {label['photo']}  run {run}  {latency:5.2f} s  {status}")

    with RESULTS.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["model"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {RESULTS.relative_to(ROOT)} and overlays in {OVERLAYS.relative_to(ROOT)}/")

    print_summary(rows)


if __name__ == "__main__":
    main()
