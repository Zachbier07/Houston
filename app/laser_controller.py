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
  M       move mode: A/D mount, W/S laser, [ ] step size, C center, M/Esc done.
          In move mode, 1 and 2 mark two opposite corners of the area to
          calibrate in (so K never sends the dot off camera); 0 clears it.
  click   aim the laser at that spot
  right-click   "the dot really landed here": learn from it and re-aim

Messages for the user go through notify(text), which must be thread-safe.
"""
import threading

import numpy as np

from .aim import CENTER_US, PAN_US, TILT_US

SAME_SPOT_PX = 40               # two tour steps closer than this count as the same spot


class LaserController:
    def __init__(self, aimer, notify, speak=None):
        self.aimer = aimer
        self.notify = notify
        self.speak = speak             # say something out loud (optional)
        self.tour = None               # {"steps": [...], "current": i} while explaining with the laser
        self.stopped = False           # set by stop(): the running tour ends at its next step
        self.frame_size = None         # (width, height) of raw camera frames
        self.cal = None                # dict while calibrating
        self.move = None               # dict while in move mode
        self.aiming = False
        self.last_target = None        # (label, x, y) last aimed at
        self.reach_px = None           # list of outlines of the reachable area, raw pixels
        if aimer.calibrated():
            self._refresh_reach()
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

    def stop(self):
        """End a pointing tour (the laser stays where it is; it can't switch off)."""
        self.stopped = True
        self.tour = None
        self.message = "Stopped."

    def explain(self, answer, steps, speak_blocking):
        """Pointing tour for an answer: say the answer, then for each step point the
        laser at it and say that step. steps: [{"label", "say", "x", "y"}] in raw
        camera pixels. speak_blocking(text) returns when the sentence is done."""
        if self.active() or not steps:
            return
        if not self.aimer.calibrated():
            speak_blocking(answer)
            self._say("I could point these out, but the laser isn't calibrated: press K.")
            return
        self.tour = tour = {"steps": steps, "current": -1}
        self.stopped = False

        def run():
            speak_blocking(answer)
            for i, st in enumerate(steps):
                if self.stopped or self.tour is not tour:
                    return
                tour["current"] = i
                if st.get("ready") is not None:        # zoom-in refinement for this step
                    for _ in range(60):                 # up to 6 s, noticing a stop within 0.1 s
                        if st["ready"].wait(timeout=0.1) or self.stopped:
                            break
                if self.stopped or self.tour is not tour:
                    return
                if st.get("resolve"):
                    st["x"], st["y"] = st["resolve"]()
                # Two different things must not end up on the same spot: if refinement
                # put this step on top of an earlier one whose first guess was
                # elsewhere, go back to this step's own first guess.
                first = st.get("first")
                if first:
                    for prev in steps[:i]:
                        pf = prev.get("first") or (prev["x"], prev["y"])
                        same_now = np.hypot(st["x"] - prev["x"], st["y"] - prev["y"]) < SAME_SPOT_PX
                        apart_before = np.hypot(first[0] - pf[0], first[1] - pf[1]) > 2 * SAME_SPOT_PX
                        if same_now and apart_before:
                            print(f"Step {i + 1} landed on step {steps.index(prev) + 1}'s spot; "
                                  "using its first guess", flush=True)
                            st["x"], st["y"] = first
                            break
                result = self.aimer.point_at(st["x"], st["y"])
                self.message = f"Step {i + 1}/{len(steps)}: {st['label']}"
                print(f"Step {i + 1}: {st['label']} -> "
                      f"{'out of reach' if result['out_of_reach'] else result.get('pulses')}", flush=True)
                say = st["say"] or st["label"]
                if result["out_of_reach"]:
                    say += " That one is outside my laser's reach."
                if self.stopped or self.tour is not tour:
                    return
                speak_blocking(say)
            self.message = f"Done explaining ({len(steps)} steps)."
            tour["current"] = len(steps)   # keep the markers up, none highlighted

        self._job(run)

    def point_at_item(self, label, x, y, box=None):
        """A find finished: point at it (if calibrated and not busy). With the object's
        box (raw pixels), a partly reachable object gets pointed at its reachable part."""
        if self.active():
            return
        if not self.aimer.calibrated():
            self._say(f"Found {label}, but the laser isn't calibrated: press K.")
            return
        self._job(lambda: self._point(label, x, y, box))

    def _refresh_reach(self):
        """Rebuild the reach outline in the background (takes ~0.5 s)."""
        def work():
            if self.frame_size is None and self.aimer.frame_size is None:
                return
            if self.aimer.frame_size is None:
                self.aimer.frame_size = self.frame_size
            self.reach_px = self.aimer.reach_outline()
        threading.Thread(target=work, daemon=True).start()

    # ---------------- output ----------------

    def status(self):
        if self.cal:
            cal = self.cal
            reply = "" if cal.get("reply") == "OK" else f" [{cal.get('reply')}]"
            return (f"CALIBRATE {cal['i'] + 1}/{len(cal['grid'])}: click the laser dot | S: can't see it | "
                    f"Backspace: back | Enter: done ({len(cal['samples'])} clicks) | Esc: cancel{reply}")
        if self.move:
            m = self.move
            corners = ("1 and 2 marked" if m.get("c1") and m.get("c2")
                       else "1 marked, now 2" if m.get("c1") else "mark corners with 1, 2")
            return (f"MOVE: A/D mount, W/S laser, [ ] step {m['step']}us, C center | {corners} | M/Esc done | "
                    f"pan {m['pan']} tilt {m['tilt']} | Uno: {m['reply']}")
        return self.message

    def draw(self, view, zoom, cv2):
        """Draw the laser overlays on a view of the camera (zoomed by `zoom`)."""
        height, width = view.shape[:2]
        crop_w, crop_h = width / zoom, height / zoom
        left, top = (width - crop_w) / 2, (height - crop_h) / 2

        def to_view(points):
            return (np.asarray(points, float) - [left, top]) * zoom

        if self.tour and not self.cal:
            for i, st in enumerate(self.tour["steps"]):
                vx, vy = to_view([(st["x"], st["y"])])[0]
                current = i == self.tour["current"]
                color = (0, 230, 0) if current else (0, 200, 255)
                cv2.circle(view, (int(vx), int(vy)), 14 if current else 10, color, 3)
                cv2.putText(view, f"{i + 1} {st['label'][:18]}", (int(vx) + 16, int(vy) - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        if self.cal:
            for px, py in self.cal["samples"].values():
                vx, vy = to_view([(px, py)])[0]
                cv2.circle(view, (int(vx), int(vy)), 6, (0, 220, 0), 2)
        elif self.reach_px:
            polys = [np.clip(to_view(c), -10000, 10000).astype(np.int32).reshape(-1, 1, 2)
                     for c in self.reach_px]
            cv2.polylines(view, polys, True, (255, 160, 0), 2, cv2.LINE_AA)

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

    def _point(self, label, x, y, box=None):
        self.last_target = (label, x, y)
        result = self.aimer.point_at_box(box) if box else self.aimer.point_at(x, y)
        if result.get("partial"):
            label = f"the reachable part of {label}"
        if result["out_of_reach"]:
            self._say(f"{label} is outside the laser's reach (outside the blue outline).")
            if self.speak:
                self.speak(f"The {label} is outside my laser's reach.")
        elif result["ok"]:
            if result.get("verified"):
                tries = result["tries"]
                self.message = (f"Laser on {label}: camera confirms {result['error_px']:.0f} px off"
                                f"{f' after {tries} corrections' if tries > 1 else ''}.")
            else:
                self.message = f"Laser on {label} (camera couldn't confirm the dot; using the model's aim)."
            print(self.message, flush=True)
        else:
            self._say(f"Laser couldn't aim at {label}: {result.get('reply')}")

    def _learn(self, x, y):
        if self.aimer.last_pulses is None:
            self._say("Aim somewhere first, then right-click where the dot really is.")
            return
        summary = self.aimer.add_sample(*self.aimer.last_pulses, x, y)
        self._refresh_reach()
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
        if self.aimer.cal_area:
            print(f"Calibrating inside the marked area {self.aimer.cal_area}", flush=True)
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
            self._refresh_reach()
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
        elif key in ("1", "2"):
            m["c" + key] = (m["pan"], m["tilt"])
            if m.get("c1") and m.get("c2"):
                self.aimer.cal_area = (m["c1"], m["c2"])
                self._say(f"Calibration area set: pan {m['c1'][0]}-{m['c2'][0]}, tilt {m['c1'][1]}-{m['c2'][1]}. "
                          "Press M to leave move mode, then K.")
        elif key == "0":
            m.pop("c1", None)
            m.pop("c2", None)
            self.aimer.cal_area = None
            self._say("Calibration area cleared: K will sweep the full servo range.")
        elif key == "[":
            m["step"] = max(5, m["step"] // 2)
        elif key == "]":
            m["step"] = min(200, m["step"] * 2)
        elif k in ("m", "Escape"):
            self.move = None
            self._say(f"Move mode off at pan {m['pan']} tilt {m['tilt']}.")
        return True   # move mode owns the keyboard
