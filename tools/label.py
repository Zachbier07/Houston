# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python"]
# ///
"""
RUNS ON: laptop. Throwaway bake-off tool, not part of app/.

Label each photo in data/bakeoff/photos/:
  1. Type the question in the terminal.
  2. Type the expected action: find, guide, check or speak.
  3. Drag a box around the right answer, then press ENTER or SPACE.
     (guide: box the FIRST step's target. speak: press C or ESC for no box.)

Saves after every photo to data/bakeoff/labels.json and skips photos that
are already labeled, so you can quit (Ctrl+C) and resume.

Usage:
  uv run tools/label.py
"""
import json
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
BAKEOFF = ROOT / "data" / "bakeoff"
PHOTOS = BAKEOFF / "photos"
LABELS = BAKEOFF / "labels.json"
ACTIONS = ("find", "guide", "check", "speak")
WINDOW = "label"


def load_labels():
    if LABELS.exists():
        return json.loads(LABELS.read_text())
    return []


def save_labels(labels):
    LABELS.write_text(json.dumps(labels, indent=2))


def ask_action():
    while True:
        action = input(f"Expected action {ACTIONS}: ").strip().lower()
        if action in ACTIONS:
            return action
        print("  Type one of: " + ", ".join(ACTIONS))


def main():
    photos = sorted(PHOTOS.glob("*.jpg"))
    if not photos:
        raise SystemExit(f"No photos in {PHOTOS}. Run tools/capture.py first.")

    labels = load_labels()
    done = {entry["photo"] for entry in labels}
    todo = [p for p in photos if p.name not in done]
    print(f"{len(done)} labeled, {len(todo)} to go.")

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    for path in todo:
        image = cv2.imread(str(path))
        cv2.imshow(WINDOW, image)
        cv2.waitKey(1)  # let the window draw before we block on input()

        print(f"\n--- {path.name} ---")
        question = input("Question: ").strip()
        action = ask_action()

        print("Drag a box around the right answer, then ENTER/SPACE (C or ESC = no box).")
        x, y, w, h = cv2.selectROI(WINDOW, image, showCrosshair=True)
        box = [int(x), int(y), int(x + w), int(y + h)] if w > 0 and h > 0 else None
        if box is None and action != "speak":
            print("  Warning: no box for a non-speak action. This photo can never score a hit.")

        labels.append({
            "photo": path.name,
            "question": question,
            "action": action,
            "box": box,  # [x1, y1, x2, y2] in full-size pixels, or null
        })
        save_labels(labels)
        print(f"  Saved. box={box}")

    cv2.destroyAllWindows()
    print(f"\nAll {len(labels)} photos labeled -> {LABELS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
