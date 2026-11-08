"""RUNS ON: laptop. Turns a pixel in the camera image into servo pulses.

No camera feedback: the laser can't be switched off, and finding the dot
automatically is unreliable (reflections, objects at different heights). So:

  1. Calibrate by hand. The laser steps through a grid of servo positions and
     you click the dot in the camera window at each one (or skip it).
  2. Fit a model of the real pan-tilt geometry to those clicks:
        pan and tilt angle  ->  laser ray direction d (3D)
        d  ->  camera pixel   via a 3x3 homography H   (pixel ~ H @ d)
     That second step is exact for any spot on the table plane: the ray from the
     laser pivot to the table, then the table seen by the camera, are both
     projective maps. This is why the dot moves in arcs, and the model
     reproduces those arcs instead of approximating them.
  3. Aim = invert the model, plus a small smooth correction fitted to what the
     model still gets wrong (lens distortion, beam offset, servo quirks). A
     target is out of reach when the pulses it needs are outside the servo limits.
  4. Learn from misses: add_sample() takes "I aimed with these pulses, the dot
     landed HERE" (a right-click in the app) and refits.

Cheap servos have gear slop (backlash): the same command lands in a slightly
different place depending on the direction you came from. move_to() always
approaches from the same side, so calibration and aiming agree.

Only valid for points on the table surface. A tall object will be hit
slightly off (parallax between the laser and the camera).

Inputs it needs:
  link  anything with aim_to(pan_us, tilt_us)   (app/serial_link.py)

All pixel coordinates are in the raw camera frame (no digital zoom).
"""
import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares

log = logging.getLogger("aim")

# ============================ TUNING ============================
# Servo limits in pulse microseconds. KEEP EQUAL to PAN_MIN/MAX_US and
# TILT_MIN/MAX_US in firmware/src/main.cpp.
PAN_US = (699, 2245)            # 15..165 deg
TILT_US = (1200, 1800)
CENTER_US = (1472, 1500)

CAL_GRID = (6, 5)               # pan x tilt calibration points (skip the ones you can't see)
APPROACH_US = 40                # come at every position from this far below, to take up gear slop
APPROACH_WAIT_S = 0.15
MIN_CORRECTION_SAMPLES = 10     # the smooth correction needs at least this many clicks
PARTIAL_MARGIN_PX = 15          # partly-reachable object: aim at least this far inside the reach area
REACH_TOLERANCE_PX = 12         # past a servo limit but the clamped aim lands this close: still reachable
MAX_FIT_ERROR_PX = 40           # refuse (and keep the old calibration) if the fit is worse than this
CORRECTION_RIDGE = 0.001        # how hard the correction is pulled toward "no correction"
                                # (tested in tools/aim_sim.py: lower overfits sloppy clicks)
MIN_CAL_POINTS = 8              # the model has 13 numbers; 8 clicks is the bare minimum
US_PER_RAD = 1856 / np.pi       # Servo library: 544..2400 us = 0..180 degrees (starting guess)

# Camera check after aiming (only if the app gives us camera frames).
# The laser can't turn off, so: park it far from the target, grab a reference
# frame, aim, grab another, and look for the dot ONLY near the target.
VERIFY = True
SETTLE_S = 0.5                  # servo travel + camera delay before grabbing a frame
SEARCH_RADIUS_PX = 70           # look for the dot this far around the target
PARK_MIN_DIST_PX = 250          # the parked dot must be at least this far from the target
DOT_MIN_JUMP = 45               # summed B+G+R brightness jump the dot must make
DOT_MAX_AREA_PX = 400           # bigger blobs are a hand / shadow / lighting change
VERIFY_TRIES = 3
HIT_TOLERANCE_PX = 6
CORRECTION_GAIN = 0.9

CALIBRATION_FILE = Path(__file__).resolve().parent.parent / "data" / "aim_calibration.json"
# ================================================================


def _direction(pan_us, tilt_us, k_pan, k_tilt, tilt0, beta=0.0, alpha=0.0):
    """Unit laser direction for servo pulses. Pan swings around the vertical axis,
    tilt swings the beam up/down. Two build imperfections of real pan-tilt mounts:
      beta   the laser isn't exactly perpendicular to the tilt axis (few degrees)
      alpha  the tilt axis isn't exactly square to the pan axis (few degrees)
    Both matter a lot when the beam points nearly straight down. (beta = alpha = 0
    is the ideal mount.)"""
    th = (np.asarray(pan_us, float) - CENTER_US[0]) / k_pan
    ph = (np.asarray(tilt_us, float) - CENTER_US[1]) / k_tilt + tilt0
    th, ph = np.broadcast_arrays(th, ph)
    # laser in the tilt frame, beta off perpendicular to the tilt axis
    bx, by, bz = np.full(th.shape, np.sin(beta)), np.full(th.shape, np.cos(beta)), np.zeros(th.shape)
    # tilt: rotate about x
    c, sn = np.cos(ph), np.sin(ph)
    by, bz = c * by - sn * bz, sn * by + c * bz
    # tilt axis tipped by alpha: rotate about y
    ca, sa = np.cos(alpha), np.sin(alpha)
    bx, bz = ca * bx + sa * bz, -sa * bx + ca * bz
    # pan: rotate about z
    ct, st = np.cos(th), np.sin(th)
    return np.stack([ct * bx + st * by, -st * bx + ct * by, bz], axis=-1)


def _project(H, d):
    """Directions (N, 3) -> pixels (N, 2)."""
    p = d @ H.T
    return p[:, :2] / p[:, 2:3]


def _fit_homography(d, px):
    """Direct linear transform: H with px ~ H @ d. Returns H or None."""
    rows = []
    for (x, y, z), (u, v) in zip(d, px):
        rows.append([x, y, z, 0, 0, 0, -u * x, -u * y, -u * z])
        rows.append([0, 0, 0, x, y, z, -v * x, -v * y, -v * z])
    try:
        _, _, vt = np.linalg.svd(np.array(rows))
    except np.linalg.LinAlgError:
        return None
    return vt[-1].reshape(3, 3)


def _poly(x, y):
    """Smooth 2nd-order terms of normalized pixel coordinates, for the correction."""
    x, y = np.atleast_1d(x), np.atleast_1d(y)
    return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1)


class Aimer:
    def __init__(self, link, calibration_file=CALIBRATION_FILE, get_frame=None):
        self.link = link
        self.get_frame = get_frame   # latest RAW camera frame, or None = no camera check
        self.calibration_file = Path(calibration_file)
        self.params = None       # k_pan, k_tilt, tilt0, H (3x3), correction
        self.frame_size = None
        self.rms_px = None
        self.samples = []        # every (pan, tilt, x, y) the model was fitted on
        self.last_pulses = None  # where the servos were sent last
        self.cal_area = None     # ((pan, tilt), (pan, tilt)): two opposite corners to calibrate inside
        self.load()

    def move_to(self, pan, tilt):
        """Move the servos, always arriving from the same side (takes up gear slop)."""
        pan, tilt = int(round(pan)), int(round(tilt))
        self.link.aim_to(max(pan - APPROACH_US, PAN_US[0]), max(tilt - APPROACH_US, TILT_US[0]))
        time.sleep(APPROACH_WAIT_S)
        reply = self.link.aim_to(pan, tilt)
        self.last_pulses = (pan, tilt)
        return reply

    # ---------------- calibration ----------------

    def calibration_grid(self):
        """(pan, tilt) pulses to visit, snaking so the servos never jump far.
        Inside the marked area (cal_area) if there is one, else the full servo range."""
        if self.cal_area:
            (p1, t1), (p2, t2) = self.cal_area
            pan_range, tilt_range = sorted((p1, p2)), sorted((t1, t2))
        else:
            pan_range, tilt_range = PAN_US, TILT_US
        pans = np.linspace(*pan_range, CAL_GRID[0]).round().astype(int)
        tilts = np.linspace(*tilt_range, CAL_GRID[1]).round().astype(int)
        grid = []
        for row, tilt in enumerate(tilts):
            for pan in (pans if row % 2 == 0 else pans[::-1]):
                grid.append((int(pan), int(tilt)))
        return grid

    def fit(self, samples, frame_size):
        """samples: list of (pan_us, tilt_us, x_px, y_px) from clicks. Saves and
        returns a summary string. Raises if there aren't enough points."""
        if len(samples) < MIN_CAL_POINTS:
            raise RuntimeError(f"Need at least {MIN_CAL_POINTS} clicked points, have {len(samples)}.")
        backup = (self.params, self.frame_size, self.rms_px, self.samples)
        s = np.array(samples, float)
        pans, tilts, px = s[:, 0], s[:, 1], s[:, 2:4]
        width, height = frame_size
        scale = np.array([width, height])
        pxn = px / scale          # fit in 0..1 units so the numbers stay well behaved

        # 1) Starting guess: servo scale from the datasheet, try many tilt offsets,
        #    solve H linearly for each, keep the best.
        guesses = []
        for tilt0 in np.radians(np.arange(-175, 180, 5)):   # incl. past vertical
            for k_tilt in (US_PER_RAD, -US_PER_RAD):
                for beta in np.radians((-20, -10, 0, 10, 20)):
                    for alpha in np.radians((-8, 0, 8)):
                        d = _direction(pans, tilts, US_PER_RAD, k_tilt, tilt0, beta, alpha)
                        H = _fit_homography(d, pxn)
                        if H is None or abs(H[2, 2]) < 1e-12:
                            continue
                        err = np.mean(np.linalg.norm(_project(H, d) - pxn, axis=1))
                        if np.isfinite(err):
                            guesses.append((err, k_tilt, tilt0, beta, alpha, H))
        if not guesses:
            raise RuntimeError("Couldn't fit the clicks. Try again with more spread-out points.")

        # 2) Refine everything together: servo scales, tilt offset, H, and the two
        #    mount angles (laser off-perpendicular, tilt axis off-square).
        def residuals(v):
            d = _direction(pans, tilts, v[0], v[1], v[2], v[11], v[12])
            Hv = np.append(v[3:11], 1.0).reshape(3, 3)
            return (_project(Hv, d) - pxn).ravel()

        # Refine the best few starting guesses and keep the best result: with 13
        # numbers a single start can get stuck in a wrong solution.
        guesses.sort(key=lambda g: g[0])
        result = None
        for _, k_tilt, tilt0, beta, alpha, H in guesses[:8]:
            H = H / H[2, 2]
            x0 = np.concatenate([[US_PER_RAD, k_tilt, tilt0], H.ravel()[:8], [beta, alpha]])
            try:
                r = least_squares(residuals, x0, method="trf")
            except ValueError:
                continue
            if np.isfinite(r.cost) and (result is None or r.cost < result.cost):
                result = r
        if result is None:
            raise RuntimeError("Couldn't fit the clicks. Try again with more spread-out points.")
        v = result.x
        H = np.append(v[3:11], 1.0).reshape(3, 3)

        # Store H in pixel units.
        H_px = np.diag([width, height, 1.0]) @ H
        self.params = {"k_pan": float(v[0]), "k_tilt": float(v[1]), "tilt0": float(v[2]),
                       "beta": float(v[11]), "alpha": float(v[12]),
                       "H": H_px.tolist(), "correction": None}
        self._lut = self._reach = None
        self.frame_size = (int(width), int(height))
        self.samples = [tuple(float(c) for c in row) for row in samples]

        # 3) Smooth correction on top: what pulses the model is still off by, as a
        #    gentle function of where in the image you aim. Ridge-regularized so a few
        #    noisy clicks can't bend it much.
        if len(samples) >= MIN_CORRECTION_SAMPLES:
            model = np.array([self._model_pulses(x, y) or (np.nan, np.nan) for x, y in px])
            ok = np.all(np.isfinite(model), axis=1)
            if ok.sum() >= MIN_CORRECTION_SAMPLES:
                # Near straight-down, pan is very sensitive; leave wild points out.
                ok &= np.all(np.abs(s[:, :2] - model) < 80, axis=1)
            if ok.sum() >= MIN_CORRECTION_SAMPLES:
                A = _poly(px[ok, 0] / width, px[ok, 1] / height)
                reg = CORRECTION_RIDGE * len(A) * np.eye(A.shape[1])
                reg[0, 0] = 0   # a constant offset is cheap to allow
                coef = [np.linalg.solve(A.T @ A + reg, A.T @ (s[ok, j] - model[ok, j])) for j in (0, 1)]
                self.params["correction"] = [c.tolist() for c in coef]

        # How well does the final aim map reproduce the clicks? (pulse error -> pixels)
        aimed = np.array([self.pulses_for(x, y) or (np.nan, np.nan) for x, y in px])
        landed = self.predict_pixel(aimed[:, 0], aimed[:, 1])
        err_px = np.linalg.norm(np.nan_to_num(landed - self.predict_pixel(pans, tilts), nan=999), axis=1)
        self.rms_px = float(np.sqrt(np.mean(err_px ** 2)))
        if self.rms_px > MAX_FIT_ERROR_PX:
            bad = self.rms_px
            self.params, self.frame_size, self.rms_px, self.samples = backup
            raise RuntimeError(f"Calibration fit was bad ({bad:.0f} px average error), so it was NOT saved; "
                               "the previous calibration is still in use. Redo K and only click the dot "
                               "when you can clearly see it.")
        self.save()
        worst = int(np.argmax(err_px))
        summary = (f"Calibrated from {len(samples)} clicks: average error {self.rms_px:.1f} px "
                   f"(worst {err_px[worst]:.1f} px at pan {int(pans[worst])} tilt {int(tilts[worst])})"
                   f"{', with correction' if self.params['correction'] else ''}; "
                   f"mount: laser {np.degrees(self.params['beta']):+.1f} deg, "
                   f"tilt axis {np.degrees(self.params['alpha']):+.1f} deg).")
        log.info(summary)
        return summary

    def add_sample(self, pan, tilt, x, y):
        """The servos were at (pan, tilt) and the dot really landed at pixel (x, y).
        Add that as a calibration point and refit. Returns the summary."""
        return self.fit(self.samples + [(pan, tilt, x, y)], self.frame_size)

    # ---------------- model ----------------

    def calibrated(self):
        return self.params is not None

    def predict_pixel(self, pan_us, tilt_us):
        """Servo pulses -> where the dot lands in the camera image (on the table plane)."""
        p = self.params
        d = _direction(np.atleast_1d(pan_us), np.atleast_1d(tilt_us), p["k_pan"], p["k_tilt"],
                       p["tilt0"], p.get("beta", 0.0), p.get("alpha", 0.0))
        return _project(np.array(p["H"]), d.reshape(-1, 3))

    def pulses_for(self, x, y):
        """Pixel -> (pan_us, tilt_us), or None if no servo position lands there."""
        pulses = self._model_pulses(x, y)
        corr = self.params.get("correction")
        if pulses is None or not corr:
            return pulses
        f = _poly(x / self.frame_size[0], y / self.frame_size[1])[0]
        return pulses[0] + f @ np.array(corr[0]), pulses[1] + f @ np.array(corr[1])

    def _table(self):
        """Where a dense grid of servo positions lands (a little past the limits too).
        Used as starting points for the pixel -> pulses solve."""
        if getattr(self, "_lut", None) is None:
            pans = np.linspace(PAN_US[0] - 150, PAN_US[1] + 150, 90)
            tilts = np.linspace(TILT_US[0] - 150, TILT_US[1] + 150, 70)
            P = np.array([(a, b) for b in tilts for a in pans])
            X = self.predict_pixel(P[:, 0], P[:, 1])
            ok = np.all(np.isfinite(X), axis=1)
            self._lut = (P[ok], X[ok])
        return self._lut

    def _model_pulses(self, x, y):
        """Pixel -> pulses from the geometry model alone (no correction). Solved
        numerically: start from the nearest table entries (spread out, so both
        "wedges" near straight-down are tried) and refine. Keeps the answer inside
        the servo limits when there is one."""
        P, X = self._table()
        order = np.argsort(np.hypot(X[:, 0] - x, X[:, 1] - y))
        starts = []
        for i in order[:200]:
            if all(np.hypot(*(P[i] - P[j])) > 80 for j in starts):
                starts.append(i)
            if len(starts) == 5:
                break
        target = np.array([x, y], float)
        best = None
        for i in starts:
            sol = least_squares(lambda q: self.predict_pixel(q[0], q[1])[0] - target, P[i],
                                method="lm", max_nfev=60)
            pan, tilt = sol.x
            back = self.predict_pixel(pan, tilt)[0]
            miss = np.hypot(*(back - target))
            outside = (max(0, PAN_US[0] - pan, pan - PAN_US[1])
                       + max(0, TILT_US[0] - tilt, tilt - TILT_US[1]))
            # Exact (<2 px) beats close; inside the limits beats outside; then closest.
            # Right under the pivot pan barely moves the dot, so "close" is accepted.
            score = (not np.isfinite(miss) or miss > 2, outside, miss if np.isfinite(miss) else 1e9)
            if best is None or score < best[0]:
                best = (score, pan, tilt)
        if best is None:
            return None
        (_, outside, miss), pan, tilt = best
        if miss > REACH_TOLERANCE_PX:
            return None
        if outside > 0:
            # Just past a limit? The clamped position may still land on the target.
            cp = min(max(pan, PAN_US[0]), PAN_US[1])
            ct = min(max(tilt, TILT_US[0]), TILT_US[1])
            back = self.predict_pixel(cp, ct)[0]
            if np.hypot(back[0] - x, back[1] - y) <= REACH_TOLERANCE_PX:
                return cp, ct
        return pan, tilt

    def _reach_mask(self):
        """Image of everywhere the dot can land, built FORWARD from a dense sweep of
        every allowed servo position (so it can't miss reachable spots). 1/4 scale."""
        if getattr(self, "_reach", None) is None:
            width, height = self.frame_size
            pans = np.linspace(*PAN_US, 220)
            tilts = np.linspace(*TILT_US, 160)
            P = np.array([(a, b) for b in tilts for a in pans])
            X = self.predict_pixel(P[:, 0], P[:, 1]) / 4
            mask = np.zeros((height // 4 + 1, width // 4 + 1), np.uint8)
            ok = (np.all(np.isfinite(X), axis=1) & (X[:, 0] >= 0) & (X[:, 1] >= 0)
                  & (X[:, 0] < mask.shape[1]) & (X[:, 1] < mask.shape[0]))
            r = max(1, int(round(REACH_TOLERANCE_PX / 4)))
            for px, py in X[ok].astype(int):
                cv2.circle(mask, (int(px), int(py)), r, 255, -1)
            self._reach = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        return self._reach

    def reachable(self, x, y):
        """True if some allowed servo position puts the dot at pixel (x, y)."""
        mask = self._reach_mask()
        i, j = int(y // 4), int(x // 4)
        return 0 <= i < mask.shape[0] and 0 <= j < mask.shape[1] and bool(mask[i, j])

    def reach_outline(self):
        """Outlines (list of (N, 2) pixel arrays) of the reachable area, for drawing."""
        contours, _ = cv2.findContours(self._reach_mask(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        return [c.reshape(-1, 2) * 4 + 2 for c in contours if len(c) >= 3]

    # ---------------- aiming ----------------

    def point_at(self, x, y):
        """Aim at pixel (x, y), then (if we have camera frames) check where the dot
        really is and correct. Returns a dict:
            ok            the Uno accepted the move
            out_of_reach  no servo position can reach (x, y)
            pulses        (pan, tilt) sent last
            verified      the camera saw the dot near the target
            error_px      distance from the seen dot to the target (if verified)
            tries         camera-corrected attempts used
        """
        if not self.calibrated():
            raise RuntimeError("Not calibrated. Press K first.")
        if not self.reachable(x, y):
            return {"ok": False, "out_of_reach": True, "pulses": None, "verified": False}

        check = VERIFY and self.get_frame is not None
        reference = self._park_and_grab(x, y) if check else None

        aim_x, aim_y = float(x), float(y)
        result = {"ok": False, "out_of_reach": False, "pulses": None, "verified": False,
                  "error_px": None, "tries": 0}
        for attempt in range(1, (VERIFY_TRIES if reference is not None else 1) + 1):
            pulses = self.pulses_for(aim_x, aim_y)
            if pulses is None:
                if attempt == 1:
                    result["out_of_reach"] = True
                break
            pan, tilt = (int(round(v)) for v in pulses)
            pan, tilt = min(max(pan, PAN_US[0]), PAN_US[1]), min(max(tilt, TILT_US[0]), TILT_US[1])
            reply = self.move_to(pan, tilt)
            result.update(ok=reply == "OK", pulses=(pan, tilt), reply=reply, tries=attempt)
            if reference is None:
                break
            time.sleep(SETTLE_S)
            frame = self.get_frame()
            dot = None if frame is None else find_dot_near(reference, frame, x, y)
            if dot is None:
                log.info("Camera check: no confident dot near the target (try %d)", attempt)
                break   # keep the model's aim; never worse than without the check
            dx, dy = x - dot[0], y - dot[1]
            error = float(np.hypot(dx, dy))
            result.update(verified=True, error_px=error, dot=dot)
            log.info("Camera check try %d: dot at (%.0f, %.0f), %.1f px off", attempt, dot[0], dot[1], error)
            if error <= HIT_TOLERANCE_PX:
                break
            aim_x += CORRECTION_GAIN * dx
            aim_y += CORRECTION_GAIN * dy
        return result

    def point_at_box(self, box):
        """Aim at an object's box (x1, y1, x2, y2, raw pixels): its center if the
        laser can reach it, otherwise the reachable spot in the box closest to the
        center. Out of reach only if no part of the box is reachable."""
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if self.reachable(cx, cy):
            return self.point_at(cx, cy)
        # 9 x 9 spots across the box, inset a little so we hit the object, not its edge
        best = None
        for fx in np.linspace(0.1, 0.9, 9):
            for fy in np.linspace(0.1, 0.9, 9):
                x, y = x1 + fx * (x2 - x1), y1 + fy * (y2 - y1)
                m = PARTIAL_MARGIN_PX   # keep off the very edge of reach, where the servos are at a limit
                if self.reachable(x, y) and all(self.reachable(x + dx, y + dy)
                                                for dx, dy in ((m, 0), (-m, 0), (0, m), (0, -m))):
                    d = np.hypot(x - cx, y - cy)
                    if best is None or d < best[0]:
                        best = (d, x, y)
        if best is None:
            return {"ok": False, "out_of_reach": True, "pulses": None, "verified": False}
        result = self.point_at(best[1], best[2])
        result["partial"] = True     # aimed at the reachable part, not the center
        return result

    def _park_and_grab(self, x, y):
        """Move the dot far from (x, y) and grab a reference frame of the target area
        without the dot in it. Returns the frame, or None if there's no safe spot."""
        # Fast path: if the dot is already far from the target (wherever the last aim
        # left it), the current camera frame already works as the reference.
        if self.last_pulses is not None:
            here = self.predict_pixel(*self.last_pulses)[0]
            if np.all(np.isfinite(here)) and np.hypot(here[0] - x, here[1] - y) >= PARK_MIN_DIST_PX:
                frame = self.get_frame()
                return None if frame is None else frame.copy()
        corners = [(PAN_US[a], TILT_US[b]) for a in (0, 1) for b in (0, 1)]
        candidates = corners + [((PAN_US[0] + PAN_US[1]) // 2, t) for t in TILT_US]
        best = None
        for pan, tilt in candidates:
            px = self.predict_pixel(pan, tilt)[0]
            dist = np.hypot(px[0] - x, px[1] - y) if np.all(np.isfinite(px)) else 1e9
            if best is None or dist > best[0]:
                best = (dist, pan, tilt)
        if best is None or best[0] < PARK_MIN_DIST_PX:
            return None
        self.move_to(best[1], best[2])
        time.sleep(SETTLE_S)
        frame = self.get_frame()
        return None if frame is None else frame.copy()

    # ---------------- save / load ----------------

    def save(self):
        self.calibration_file.parent.mkdir(parents=True, exist_ok=True)
        self.calibration_file.write_text(json.dumps({
            "params": self.params,
            "frame_size": list(self.frame_size),
            "rms_px": self.rms_px,
            "samples": self.samples,
            "limits": {"pan_us": PAN_US, "tilt_us": TILT_US},
            "cal_area": self.cal_area,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2))

    def load(self):
        if not self.calibration_file.exists():
            return False
        data = json.loads(self.calibration_file.read_text())
        area = data.get("cal_area")
        self.cal_area = tuple(tuple(c) for c in area) if area else None
        if "params" not in data:   # file from the old dot-finding version
            return False
        self.params = data["params"]
        self.frame_size = tuple(data["frame_size"])
        self._lut = self._reach = None
        self.rms_px = data.get("rms_px")
        self.samples = [tuple(row) for row in data.get("samples", [])]
        log.info("Loaded calibration from %s (%s)", self.calibration_file, data.get("saved_at"))
        return True


def find_dot_near(reference, frame, x, y, radius=SEARCH_RADIUS_PX):
    """Find the laser dot within `radius` px of (x, y): the spot that got much
    brighter and redder between `reference` (laser parked elsewhere) and `frame`.
    Returns (x, y) in frame pixels, or None if there's no confident single dot."""
    h, w = frame.shape[:2]
    x0, x1 = int(max(x - radius, 0)), int(min(x + radius + 1, w))
    y0, y1 = int(max(y - radius, 0)), int(min(y + radius + 1, h))
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None
    ref = reference[y0:y1, x0:x1]
    cur = frame[y0:y1, x0:x1]
    # LED lamps and auto-exposure shift overall brightness between frames.
    gain = (float(ref.mean()) + 1) / (float(cur.mean()) + 1)
    if abs(gain - 1) > 0.002:
        cur = cv2.convertScaleAbs(cur, alpha=gain)
    b, g, r = cv2.split(cv2.subtract(cur, ref))
    jump = cv2.GaussianBlur(cv2.add(cv2.add(b, g), r), (5, 5), 0)
    peak = int(jump.max())
    noise = float(np.percentile(jump, 90))
    if peak < DOT_MIN_JUMP or peak < 3 * noise + 15:
        return None
    _, mask = cv2.threshold(jump, max(DOT_MIN_JUMP * 0.6, peak * 0.5), 255, cv2.THRESH_BINARY)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)
    blobs = [i for i in range(1, count) if 2 <= stats[i, cv2.CC_STAT_AREA] <= DOT_MAX_AREA_PX]
    if len(blobs) != 1:
        return None   # nothing, or several candidates: don't guess
    i = blobs[0]
    # A red laser adds mostly red. Reject spots that got brighter without getting redder.
    ys, xs = np.nonzero(mask == 255)
    sel = [(yy, xx) for yy, xx in zip(ys, xs)
           if stats[i, cv2.CC_STAT_LEFT] <= xx < stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH]
           and stats[i, cv2.CC_STAT_TOP] <= yy < stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]]
    if sel:
        yy, xx = np.array(sel).T
        if float(r[yy, xx].mean()) < 0.8 * max(float(g[yy, xx].mean()), float(b[yy, xx].mean())):
            return None
    cx, cy = centroids[i]
    return float(cx + x0), float(cy + y0)
