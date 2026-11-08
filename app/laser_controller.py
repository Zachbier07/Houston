"""RUNS ON: laptop. Laser features for the camera window, independent of the UI toolkit.

The window (app/tk_camera_app.py) forwards three things here:
  - key presses        handle_key("k"), handle_key("w"), ...  (returns True if used)
  - clicks on the image click(x_raw, y_raw, right=False)       raw camera pixels
  - finished finds     point_at_item(label, x_raw, y_raw)

and asks for two things back:
  - status()           one line describing what the laser is doing
  - draw(view, zoom)   overlays on the camera image (reach outline, calibration clicks)

Features:
  K       calibrate: the laser steps through a grid; click the dot each time
          (S skip, Backspace back, Enter finish early, Esc cancel)
  M       move mode: A/D mount, W/S laser, [ ] step size, C center, M/Esc done
  click   aim the laser at that spot
  right-click   "the dot really landed here": learn from it and re-aim

Messages for the user go through notify(text), which must be thread-safe.
"""
import threading

import numpy as np

from .aim import CENTER_US, PAN_US, TILT_US


class LaserController:
    def __init__(self, aimer, notify):
        self.aimer = aimer
        self.notify = notify
        self.frame_size = None         # (width, height) of raw camera frames
        self.cal = None                # dict while calibrating
        self.move = None               # dict while in move mode
        self.aiming = False
        self.last_target = None        # (label, x, y) last aimed at
        self.reach_px = aimer.reach_outline() if aimer.calibrated() else None
        self.message = "Laser ready." if aimer.calibrated() else "Laser not calibrated: press K."

    def active(self):
        """True while calibrating or moving: those modes own the keyboard."""
        return self.cal is not None or self.move is not None

    # ---------------- input ----------------

    def handle_key(self, key):
        """key: a single character, or "Escape", "Return", "BackSpace". Returns True if used."""
        if self.cal:
            return self._cal_key(key)
        if self.move:
            return self._move_key(key)
        if key in ("k", "K"):
            self.start_cal()
            return True
        if key in ("m", "M"):
            self.start_move()
            return True
        return False

    def click(self, x, y, right=False):
        """A click on the camera image, in raw camera pixels."""
        if self.cal:
            if not right:
                self.cal["samples"][self.cal["i"]] = (x, y)
                self._cal_next()
            return
        if self.move or not self.aimer.calibrated():
            if not self.aimer.calibrated():
                self._say("Laser not calibrated: press K first.")
            return
        if right:
            self._job(lambda: self._learn(x, y))
        else:
            self._job(lambda: self._point(f"({x:.0f}, {y:.0f})", x, y))

    def point_at_item(self, label, x, y):
        """A find finished: point at it (if calibrated and not busy)."""
        if self.active():
            return
        if not self.aimer.calibrated():
            self._say(f"Found {label}, but the laser isn't calibrated: press K.")
            return
        self._job(lambda: self._point(label, x, y))

    # ---------------- output ----------------

    def status(self):
        if self.cal:
            cal = self.cal
            reply = "" if cal.get("reply") == "OK" else f" [{cal.get('reply')}]"
            return (f"CALIBRATE {cal['i'] + 1}/{len(cal['grid'])}: click the laser dot | S: can't see it | "
                    f"Backspace: back | Enter: done ({len(cal['samples'])} clicks) | Esc: cancel{reply}")
        if self.move:
            m = self.move
            return (f"MOVE: A/D mount, W/S laser, [ ] step {m['step']}us, C center, M/Esc done | "
                    f"pan {m['pan']} tilt {m['tilt']} | Uno: {m['reply']}")
        return self.message

    def draw(self, view, zoom, cv2):
        """Draw the laser overlays on a view of the camera (zoomed by `zoom`)."""
        height, width = view.shape[:2]
        crop_w, crop_h = width / zoom, height / zoom
        left, top = (width - crop_w) / 2, (height - crop_h) / 2

        def to_view(points):
            return (np.asarray(points, float) - [left, top]) * zoom

        if self.cal:
            for px, py in self.cal["samples"].values():
                vx, vy = to_view([(px, py)])[0]
                cv2.circle(view, (int(vx), int(vy)), 6, (0, 220, 0), 2)
        elif self.reach_px is not None:
            pts = to_view(self.reach_px)
            if np.all(np.isfinite(pts)):
                poly = np.clip(pts, -10000, 10000).astype(np.int32).reshape(-1, 1, 2)
                cv2.polylines(view, [poly], True, (255, 160, 0), 2, cv2.LINE_AA)

    # ---------------- aiming ----------------

    def _say(self, text):
        self.message = text
        self.notify(text)
        print(text, flush=True)

    def _job(self, fn):
        """Run a laser job off the UI thread, one at a time."""
        if self.aiming:
            return
        self.aiming = True

        def wrapper():
            try:
                fn()
            except Exception as exc:
                self._say(f"Laser error: {exc}")
            finally:
                self.aiming = False

        threading.Thread(target=wrapper, daemon=True).start()

    def _point(self, label, x, y):
        self.last_target = (label, x, y)
        result = self.aimer.point_at(x, y)
        if result["out_of_reach"]:
            self._say(f"{label} is outside the laser's reach (outside the blue outline).")
        elif result["ok"]:
            pan, tilt = result["pulses"]
            self.message = f"Laser on {label} (pan {pan}, tilt {tilt})."
            print(self.message, flush=True)
        else:
            self._say(f"Laser couldn't aim at {label}: {result.get('reply')}")

    def _learn(self, x, y):
        if self.aimer.last_pulses is None:
            self._say("Aim somewhere first, then right-click where the dot really is.")
            return
        summary = self.aimer.add_sample(*self.aimer.last_pulses, x, y)
        self.reach_px = self.aimer.reach_outline()
        print(f"Learned from correction: {summary}", flush=True)
        if self.last_target:
            label, tx, ty = self.last_target
            self._point(label, tx, ty)
        self.message = f"Learned ({len(self.aimer.samples)} points) and re-aimed."

    # ---------------- calibration ----------------

    def start_cal(self):
        if self.aiming:
            return
        self.cal = {"grid": self.aimer.calibration_grid(), "i": 0, "samples": {}}
        self._cal_move()

    def _cal_move(self):
        pan, tilt = self.cal["grid"][self.cal["i"]]
        self.cal["reply"] = self.aimer.move_to(pan, tilt)

    def _cal_next(self):
        self.cal["i"] += 1
        if self.cal["i"] >= len(self.cal["grid"]):
            self._cal_finish()
        else:
            self._cal_move()

    def _cal_key(self, key):
        cal = self.cal
        if key in ("s", "S"):
            cal["samples"].pop(cal["i"], None)
            self._cal_next()
        elif key == "BackSpace" and cal["i"] > 0:
            cal["i"] -= 1
            cal["samples"].pop(cal["i"], None)
            self._cal_move()
        elif key == "Return":
            self._cal_finish()
        elif key == "Escape":
            self.cal = None
            self._say("Calibration cancelled.")
        return True   # calibration owns the keyboard

    def _cal_finish(self):
        cal, self.cal = self.cal, None
        samples = [(*cal["grid"][i], px, py) for i, (px, py) in sorted(cal["samples"].items())]
        try:
            summary = self.aimer.fit(samples, self.frame_size)
            self.reach_px = self.aimer.reach_outline()
            self._say(summary + " Click anywhere to test.")
        except Exception as exc:
            self._say(f"Calibration failed: {exc}")

    # ---------------- move mode ----------------

    def start_move(self):
        if self.aiming:
            return
        self.move = {"pan": CENTER_US[0], "tilt": CENTER_US[1], "step": 30, "reply": ""}
        self._send_move()

    def _send_move(self):
        m = self.move
        m["pan"] = min(max(m["pan"], PAN_US[0]), PAN_US[1])
        m["tilt"] = min(max(m["tilt"], TILT_US[0]), TILT_US[1])
        m["reply"] = self.aimer.link.aim_to(m["pan"], m["tilt"])
        print(f"Move: A {m['pan']} {m['tilt']} -> {m['reply']}", flush=True)

    def _move_key(self, key):
        m = self.move
        steps = {"a": (-1, 0), "d": (1, 0), "w": (0, 1), "s": (0, -1)}
        k = key.lower() if len(key) == 1 else key
        if k in steps:
            dp, dt = steps[k]
            m["pan"] += dp * m["step"]
            m["tilt"] += dt * m["step"]
            self._send_move()
        elif k == "c":
            m["pan"], m["tilt"] = CENTER_US
            self._send_move()
        elif key == "[":
            m["step"] = max(5, m["step"] // 2)
        elif key == "]":
            m["step"] = min(200, m["step"] * 2)
        elif k in ("m", "Escape"):
            self.move = None
            self._say(f"Move mode off at pan {m['pan']} tilt {m['tilt']}.")
        return True   # move mode owns the keyboard
