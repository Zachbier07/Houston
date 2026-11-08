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

import numpy as np
from scipy.optimize import least_squares

log = logging.getLogger("aim")

# ============================ TUNING ============================
# Servo limits in pulse microseconds. KEEP EQUAL to PAN_MIN/MAX_US and
# TILT_MIN/MAX_US in firmware/src/main.cpp.
PAN_US = (1008, 1936)
TILT_US = (1200, 1800)
CENTER_US = (1472, 1500)

CAL_GRID = (6, 5)               # pan x tilt calibration points (skip the ones you can't see)
APPROACH_US = 40                # come at every position from this far below, to take up gear slop
APPROACH_WAIT_S = 0.15
MIN_CORRECTION_SAMPLES = 10     # the smooth correction needs at least this many clicks
CORRECTION_RIDGE = 0.001        # how hard the correction is pulled toward "no correction"
                                # (tested in tools/aim_sim.py: lower overfits sloppy clicks)
MIN_CAL_POINTS = 6              # the model has 11 numbers; 6 clicks is the bare minimum
US_PER_RAD = 1856 / np.pi       # Servo library: 544..2400 us = 0..180 degrees (starting guess)

CALIBRATION_FILE = Path(__file__).resolve().parent.parent / "data" / "aim_calibration.json"
# ================================================================


def _direction(pan_us, tilt_us, k_pan, k_tilt, tilt0):
    """Unit laser direction for servo pulses. Pan swings around the vertical
    axis, tilt swings the beam up/down in the panned plane."""
    theta = (np.asarray(pan_us, float) - CENTER_US[0]) / k_pan
    phi = (np.asarray(tilt_us, float) - CENTER_US[1]) / k_tilt + tilt0
    return np.stack([np.cos(phi) * np.sin(theta), np.cos(phi) * np.cos(theta), np.sin(phi)], axis=-1)


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
    def __init__(self, link, calibration_file=CALIBRATION_FILE):
        self.link = link
        self.calibration_file = Path(calibration_file)
        self.params = None       # k_pan, k_tilt, tilt0, H (3x3), correction
        self.frame_size = None
        self.rms_px = None
        self.samples = []        # every (pan, tilt, x, y) the model was fitted on
        self.last_pulses = None  # where the servos were sent last
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

    @staticmethod
    def calibration_grid():
        """(pan, tilt) pulses to visit, snaking so the servos never jump far."""
        pans = np.linspace(*PAN_US, CAL_GRID[0]).round().astype(int)
        tilts = np.linspace(*TILT_US, CAL_GRID[1]).round().astype(int)
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
        s = np.array(samples, float)
        pans, tilts, px = s[:, 0], s[:, 1], s[:, 2:4]
        width, height = frame_size
        scale = np.array([width, height])
        pxn = px / scale          # fit in 0..1 units so the numbers stay well behaved

        # 1) Starting guess: servo scale from the datasheet, try many tilt offsets,
        #    solve H linearly for each, keep the best.
        best = None
        for tilt0 in np.radians(np.arange(-85, 86, 5)):
            for k_tilt in (US_PER_RAD, -US_PER_RAD):
                d = _direction(pans, tilts, US_PER_RAD, k_tilt, tilt0)
                H = _fit_homography(d, pxn)
                if H is None:
                    continue
                err = np.mean(np.linalg.norm(_project(H, d) - pxn, axis=1))
                if np.isfinite(err) and (best is None or err < best[0]):
                    best = (err, k_tilt, tilt0, H)
        if best is None:
            raise RuntimeError("Couldn't fit the clicks. Try again with more spread-out points.")
        _, k_tilt, tilt0, H = best

        # 2) Refine everything together (servo scales, tilt offset, H).
        def residuals(v):
            d = _direction(pans, tilts, v[0], v[1], v[2])
            Hv = np.append(v[3:], 1.0).reshape(3, 3)
            return (_project(Hv, d) - pxn).ravel()

        H = H / H[2, 2]
        x0 = np.concatenate([[US_PER_RAD, k_tilt, tilt0], H.ravel()[:8]])
        result = least_squares(residuals, x0, method="trf")
        v = result.x
        H = np.append(v[3:], 1.0).reshape(3, 3)

        # Store H in pixel units.
        H_px = np.diag([width, height, 1.0]) @ H
        self.params = {"k_pan": float(v[0]), "k_tilt": float(v[1]), "tilt0": float(v[2]),
                       "H": H_px.tolist(), "correction": None}
        self.frame_size = (int(width), int(height))
        self.samples = [tuple(float(c) for c in row) for row in samples]

        # 3) Smooth correction on top: what pulses the model is still off by, as a
        #    gentle function of where in the image you aim. Ridge-regularized so a few
        #    noisy clicks can't bend it much.
        if len(samples) >= MIN_CORRECTION_SAMPLES:
            model = np.array([self._model_pulses(x, y) or (np.nan, np.nan) for x, y in px])
            ok = np.all(np.isfinite(model), axis=1)
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
        self.save()
        worst = int(np.argmax(err_px))
        summary = (f"Calibrated from {len(samples)} clicks: average error {self.rms_px:.1f} px "
                   f"(worst {err_px[worst]:.1f} px at pan {int(pans[worst])} tilt {int(tilts[worst])})"
                   f"{', with correction' if self.params['correction'] else ''}.")
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
        d = _direction(np.atleast_1d(pan_us), np.atleast_1d(tilt_us), p["k_pan"], p["k_tilt"], p["tilt0"])
        return _project(np.array(p["H"]), d)

    def pulses_for(self, x, y):
        """Pixel -> (pan_us, tilt_us), or None if no servo position lands there.
        The pulses may be outside the servo limits; see reachable()."""
        pulses = self._model_pulses(x, y)
        corr = self.params.get("correction")
        if pulses is None or not corr:
            return pulses
        f = _poly(x / self.frame_size[0], y / self.frame_size[1])[0]
        return pulses[0] + f @ np.array(corr[0]), pulses[1] + f @ np.array(corr[1])

    def _model_pulses(self, x, y):
        """Pixel -> pulses from the geometry model alone (no correction)."""
        p = self.params
        d = np.linalg.solve(np.array(p["H"]), np.array([x, y, 1.0]))
        d /= np.linalg.norm(d)
        best = None
        for cand in (d, -d):   # a direction and its opposite give the same pixel; keep the real one
            phi = np.arcsin(np.clip(cand[2], -1, 1))
            theta = np.arctan2(cand[0], cand[1])
            pan = CENTER_US[0] + theta * p["k_pan"]
            tilt = CENTER_US[1] + (phi - p["tilt0"]) * p["k_tilt"]
            # sanity check: does it really map back to the target?
            back = self.predict_pixel(pan, tilt)[0]
            miss = np.hypot(back[0] - x, back[1] - y)
            outside = max(0, PAN_US[0] - pan, pan - PAN_US[1]) + max(0, TILT_US[0] - tilt, tilt - TILT_US[1])
            score = (miss > 2, outside)
            if best is None or score < best[0]:
                best = (score, pan, tilt)
        (bad_fit, _), pan, tilt = best
        return None if bad_fit else (pan, tilt)

    def reachable(self, x, y):
        pulses = self.pulses_for(x, y)
        if pulses is None:
            return False
        pan, tilt = pulses
        return PAN_US[0] <= pan <= PAN_US[1] and TILT_US[0] <= tilt <= TILT_US[1]

    def reach_outline(self, steps=40):
        """Pixels along the edge of what the servos can reach (curved), for drawing."""
        pans = np.linspace(*PAN_US, steps)
        tilts = np.linspace(*TILT_US, steps)
        edge = ([(p, TILT_US[0]) for p in pans] + [(PAN_US[1], t) for t in tilts]
                + [(p, TILT_US[1]) for p in pans[::-1]] + [(PAN_US[0], t) for t in tilts[::-1]])
        e = np.array(edge)
        return self.predict_pixel(e[:, 0], e[:, 1])

    # ---------------- aiming ----------------

    def point_at(self, x, y):
        """Aim at pixel (x, y). Returns {"ok": bool, "out_of_reach": bool, "pulses": (pan, tilt) or None}."""
        if not self.calibrated():
            raise RuntimeError("Not calibrated. Press K first.")
        if not self.reachable(x, y):
            return {"ok": False, "out_of_reach": True, "pulses": None}
        pan, tilt = (int(round(v)) for v in self.pulses_for(x, y))
        reply = self.move_to(pan, tilt)
        return {"ok": reply == "OK", "out_of_reach": False, "pulses": (pan, tilt), "reply": reply}

    # ---------------- save / load ----------------

    def save(self):
        self.calibration_file.parent.mkdir(parents=True, exist_ok=True)
        self.calibration_file.write_text(json.dumps({
            "params": self.params,
            "frame_size": list(self.frame_size),
            "rms_px": self.rms_px,
            "samples": self.samples,
            "limits": {"pan_us": PAN_US, "tilt_us": TILT_US},
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2))

    def load(self):
        if not self.calibration_file.exists():
            return False
        data = json.loads(self.calibration_file.read_text())
        if "params" not in data:   # file from the old dot-finding version
            return False
        self.params = data["params"]
        self.frame_size = tuple(data["frame_size"])
        self.rms_px = data.get("rms_px")
        self.samples = [tuple(row) for row in data.get("samples", [])]
        log.info("Loaded calibration from %s (%s)", self.calibration_file, data.get("saved_at"))
        return True
