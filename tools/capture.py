# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python"]
# ///
"""
RUNS ON: laptop. Throwaway bake-off tool, not part of app/.

Live webcam preview with the camera settings camera.py will use:
exposure, focus and white balance locked, exposure low.

Keys:
  SPACE  save a frame to data/bakeoff/photos/NN.jpg
  [ / ]  lower / raise the locked exposure (prints the value)
  Q      quit

Usage:
  uv run tools/capture.py                 # camera 0
  uv run tools/capture.py --camera 2 --exposure 80
"""
import argparse
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "data" / "bakeoff" / "photos"


def lock_camera(cap, exposure):
    """Lock exposure, focus and white balance. Prints what the camera accepted."""
    # V4L2 (Linux): 1 = manual exposure, 3 = auto. Other backends may ignore this.
    settings = [
        ("auto exposure off", cv2.CAP_PROP_AUTO_EXPOSURE, 1),
        ("exposure", cv2.CAP_PROP_EXPOSURE, exposure),
        ("autofocus off", cv2.CAP_PROP_AUTOFOCUS, 0),
        ("auto white balance off", cv2.CAP_PROP_AUTO_WB, 0),
    ]
    for name, prop, value in settings:
        ok = cap.set(prop, value)
        print(f"  {name:24s} {'ok' if ok else 'NOT SUPPORTED'} (now {cap.get(prop)})")


def next_photo_path():
    existing = sorted(PHOTOS.glob("*.jpg"))
    n = int(existing[-1].stem) + 1 if existing else 1
    return PHOTOS / f"{n:02d}.jpg"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--exposure", type=float, default=100,
                        help="locked exposure value (V4L2 units, ~100 us each)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()

    PHOTOS.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        raise SystemExit(f"Can't open camera {args.camera}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)

    print("Locking camera settings:")
    exposure = args.exposure
    lock_camera(cap, exposure)
    print("SPACE = save, [ / ] = exposure down / up, Q = quit")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("Frame grab failed")
            break

        preview = frame.copy()
        cv2.putText(preview, f"exposure {exposure:g}   SPACE save   Q quit", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        cv2.imshow("capture", preview)

        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            path = next_photo_path()
            cv2.imwrite(str(path), frame)  # save the clean frame, not the preview
            print(f"Saved {path.relative_to(ROOT)} ({frame.shape[1]}x{frame.shape[0]})")
        elif key in (ord("["), ord("]")):
            exposure = max(1, exposure * (0.8 if key == ord("[") else 1.25))
            cap.set(cv2.CAP_PROP_EXPOSURE, exposure)
            print(f"exposure -> {exposure:g} (camera reports {cap.get(cv2.CAP_PROP_EXPOSURE)})")
        elif key in (ord("q"), ord("Q")):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
