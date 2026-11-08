"""RUNS ON: laptop. Tests app/aim.py with no hardware.

A simulated pan-tilt laser over a table, seen by a simulated camera, built from
real 3D geometry with the kind of imperfections a hackathon mount has:
  - laser pivot 45 cm above the table, mount yawed and tipped a few degrees
  - servo scale a few % off the datasheet, beam starting 1.5 cm off the pivot
  - perspective camera off to the side, looking down at an angle, mild lens distortion
  - gear slop: each servo has BACKLASH_US of play, so where it ends up depends on
    the direction it came from
  - a "user" who clicks the dot up to 3 px off, and skips dots outside the view
None of those imperfections are in aim.py's model, so this is a fair test.

Usage (from the repo root):
    .venv/bin/python tools/aim_sim.py
    .venv/bin/python tools/aim_sim.py --targets 300 --seed 4 --click-noise 5
"""
import argparse
import collections
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import aim  # noqa: E402

W, H = 1280, 720
BACKLASH_US = 12


def rot(axis, deg):
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def look_at(cam, target):
    """World -> camera rotation for a camera at cam looking at target (image y points down)."""
    z = (target - cam) / np.linalg.norm(target - cam)
    x = np.cross(z, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.array([x, y, z])


class World:
    def __init__(self, rng):
        self.pivot = np.array([0.05, -0.10, 0.45])                     # meters, table is z = 0
        self.mount = rot("z", rng.uniform(-25, 25)) @ rot("x", rng.uniform(-6, 6)) @ rot("y", rng.uniform(-6, 6))
        self.k_pan = (1856 / 180) * rng.uniform(0.93, 1.07)              # us per degree
        self.k_tilt = (1856 / 180) * rng.uniform(0.93, 1.07)
        self.tilt0 = rng.uniform(-60, -40)                               # beam points down and forward
        self.beam_offset = np.array([0.015, 0, 0])                       # beam 1.5 cm to the side of the pivot
        self.beta = np.radians(rng.uniform(-4, 4))                       # laser a few degrees off perpendicular
        self.alpha = np.radians(rng.uniform(-6, 6))                      # tilt axis a few degrees off square
        # camera: 0.75 m up, behind the laser, looking down at the table
        self.cam = np.array([0.0, -0.45, 0.75])
        self.cam_R = look_at(self.cam, np.array([0.0, 0.2, 0.0]))       # world -> camera
        self.f = 950.0
        self.k1 = -0.06

    def dot_world(self, pan, tilt):
        theta = np.radians((pan - aim.CENTER_US[0]) / self.k_pan)
        d_local = aim._direction(pan, tilt, self.k_pan * 180 / np.pi, self.k_tilt * 180 / np.pi,
                                 np.radians(self.tilt0), self.beta, self.alpha).reshape(3)
        start = self.pivot + self.mount @ (rot("z", np.degrees(-theta)) @ self.beam_offset)
        d = self.mount @ d_local
        if d[2] >= -1e-6:
            return None                                                  # beam never reaches the table
        return start + d * (-start[2] / d[2])

    def to_pixel(self, X):
        p = self.cam_R @ (X - self.cam)
        if p[2] <= 0:
            return None
        x, y = p[0] / p[2], p[1] / p[2]
        r2 = x * x + y * y
        x, y = x * (1 + self.k1 * r2), y * (1 + self.k1 * r2)
        return np.array([W / 2 + self.f * x, H / 2 + self.f * y])

    def dot_pixel(self, pan, tilt):
        X = self.dot_world(pan, tilt)
        return None if X is None else self.to_pixel(X)


def backlash(out, cmd, play):
    """Gear slop: the output only moves once the input has taken up the play."""
    if cmd > out + play / 2:
        return cmd - play / 2
    if cmd < out - play / 2:
        return cmd + play / 2
    return out


class FakeLink:
    """Fake Uno + servos with gear slop. .actual is where the servos really are."""

    def __init__(self, play=BACKLASH_US):
        self.play = play
        self.last = None
        self.actual = (1472.0, 1500.0)

    def aim_to(self, pan, tilt):
        self.last = (pan, tilt)
        self.actual = (backlash(self.actual[0], pan, self.play), backlash(self.actual[1], tilt, self.play))
        return "OK"


class SimCamera:
    """Renders camera frames: a desk photo plus the laser dot where the servos
    REALLY are (gear slop included), with camera lag, flicker, noise and glints."""

    def __init__(self, world, link, photo, lag_s=0.12, glint_chance=0.05):
        self.world, self.link, self.lag_s, self.glint_chance = world, link, lag_s, glint_chance
        self.desk = cv2.resize(photo, (W, H)).astype(np.float32)
        self.history = collections.deque(maxlen=400)
        self.lock = threading.Lock()
        self.rng = np.random.default_rng(99)
        orig = link.aim_to

        def aim_to(pan, tilt):        # record when the servos moved, for the lag
            reply = orig(pan, tilt)
            with self.lock:
                self.history.append((time.monotonic(), link.actual))
            return reply
        link.aim_to = aim_to
        self.history.append((0.0, link.actual))

    def get_frame(self):
        cutoff = time.monotonic() - self.lag_s
        with self.lock:
            actual = self.history[0][1]
            for t, a in self.history:
                if t <= cutoff:
                    actual = a
        frame = self.desk.copy()
        px = self.world.dot_pixel(*actual)
        if visible(px):
            x, y = px
            x0, x1, y0, y1 = int(max(x - 12, 0)), int(min(x + 13, W)), int(max(y - 12, 0)), int(min(y + 13, H))
            yy, xx = np.mgrid[y0:y1, x0:x1]
            glow = np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 2.5 ** 2))
            frame[y0:y1, x0:x1] += glow[:, :, None] * np.array([40, 40, 200], np.float32)
        if self.rng.random() < self.glint_chance:         # a reflection flashes somewhere
            gx, gy = self.rng.integers(20, W - 20), self.rng.integers(20, H - 20)
            frame[gy - 2:gy + 3, gx - 2:gx + 3] += 150
        frame += self.rng.integers(-4, 5, frame.shape)
        frame *= self.rng.uniform(0.97, 1.03)
        return np.clip(frame, 0, 255).astype(np.uint8)


def camera_check_test(seeds, targets, photo_path, click_noise=3.0):
    """Model-only aim vs model + camera check, on rendered frames."""
    photo = cv2.imread(photo_path) if photo_path else np.full((H, W, 3), 120, np.uint8)
    aim.APPROACH_WAIT_S = 0.0
    aim.SETTLE_S = 0.3
    results = {False: [], True: []}
    confirmed = tries = 0
    for seed in range(1, seeds + 1):
        rng = np.random.default_rng(seed)
        world = World(rng)
        link = FakeLink()
        cam = SimCamera(world, link, photo)
        aimer = aim.Aimer(link, calibration_file=ROOT / "data" / "aim_calibration_sim.json")
        samples = []
        for pan, tilt in aimer.calibration_grid():
            aimer.move_to(pan, tilt)
            px = world.dot_pixel(*link.actual)
            if visible(px):
                c = px + rng.uniform(-click_noise, click_noise, 2)
                samples.append((pan, tilt, c[0], c[1]))
        aimer.fit(samples, (W, H))
        picked = 0
        while picked < targets:
            x, y = rng.uniform(0, W), rng.uniform(0, H)
            if not aimer.reachable(x, y):
                continue
            picked += 1
            for check in (False, True):
                aimer.get_frame = cam.get_frame if check else None
                r = aimer.point_at(x, y)
                landed = world.dot_pixel(*link.actual)
                results[check].append(np.hypot(landed[0] - x, landed[1] - y))
                if check:
                    confirmed += r.get("verified", False)
                    tries += r.get("tries", 0)
        (ROOT / "data" / "aim_calibration_sim.json").unlink(missing_ok=True)
    n = len(results[True])
    for check, name in ((False, "model only"), (True, "model + camera check")):
        e = np.array(results[check])
        print(f"  {name:22s} median {np.median(e):4.1f} px   90th {np.percentile(e, 90):5.1f} px   "
              f"worst {e.max():5.1f} px   within 6 px: {100 * np.mean(e <= 6):3.0f}%")
    print(f"  camera confirmed the dot on {confirmed}/{n} aims, {tries / n:.1f} moves per aim on average")


def visible(px):
    return px is not None and 0 <= px[0] < W and 0 <= px[1] < H


def run(seed, click_noise, approach=True, correction=True, learn=0, targets=200, quiet=False):
    """Calibrate, optionally learn from right-click corrections, then measure aim error."""
    rng = np.random.default_rng(seed)
    world = World(rng)
    link = FakeLink()
    aim.APPROACH_WAIT_S = 0
    saved = (aim.APPROACH_US, aim.MIN_CORRECTION_SAMPLES)
    aim.APPROACH_US = 40 if approach else 0
    aim.MIN_CORRECTION_SAMPLES = 10 if correction else 10 ** 6
    aimer = aim.Aimer(link, calibration_file=ROOT / "data" / "aim_calibration_sim.json")
    aimer.load = lambda: False

    def dot_now():
        return world.dot_pixel(*link.actual)

    def click(px):
        return px + rng.uniform(-click_noise, click_noise, 2)

    # Calibration: visit the grid, "click" the dot where it's visible.
    samples = []
    for pan, tilt in aimer.calibration_grid():
        if aim.APPROACH_US:
            aimer.move_to(pan, tilt)
        else:
            link.aim_to(pan, tilt)
        px = dot_now()
        if visible(px):
            c = click(px)
            samples.append((pan, tilt, c[0], c[1]))
    summary = aimer.fit(samples, (W, H))

    # Learning: aim at random reachable spots, "right-click" where the dot really landed.
    for _ in range(learn):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        r = aimer.point_at(x, y)
        if r["out_of_reach"]:
            continue
        px = dot_now()
        if visible(px):
            c = click(px)
            aimer.add_sample(*aimer.last_pulses, c[0], c[1])

    errors = []
    for _ in range(targets):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        r = aimer.point_at(x, y)
        if r["out_of_reach"]:
            continue
        landed = dot_now()
        if landed is not None:
            errors.append(np.hypot(landed[0] - x, landed[1] - y))
    aim.APPROACH_US, aim.MIN_CORRECTION_SAMPLES = saved
    (ROOT / "data" / "aim_calibration_sim.json").unlink(missing_ok=True)
    if not quiet:
        print(summary)
    return np.array(errors), len(samples)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=6)
    parser.add_argument("--click-noise", type=float, default=3.0, help="max click error, px")
    parser.add_argument("--camera-check", action="store_true",
                        help="test the camera check on rendered frames instead")
    parser.add_argument("--photo", help="desk photo for --camera-check")
    parser.add_argument("--targets", type=int, default=15)
    args = parser.parse_args()
    if args.camera_check:
        print(f"Camera check on rendered frames: {args.seeds} mounts x {args.targets} targets")
        camera_check_test(args.seeds, args.targets, args.photo, args.click_noise)
        return

    setups = [("old: no approach, no correction", dict(approach=False, correction=False)),
              ("+ same-side approach", dict(approach=True, correction=False)),
              ("+ smooth correction", dict(approach=True, correction=True)),
              ("+ 15 right-click corrections", dict(approach=True, correction=True, learn=15))]
    print(f"{len(range(args.seeds))} random mounts, gear slop {BACKLASH_US} us, clicks up to "
          f"{args.click_noise:.0f} px off. Aim error in pixels (lower is better):")
    print(f"  {'setup':36s} {'median':>7s} {'90th %':>7s} {'worst':>7s}")
    for name, opts in setups:
        all_err = np.concatenate([run(seed, args.click_noise, quiet=True, **opts)[0]
                                  for seed in range(1, args.seeds + 1)])
        print(f"  {name:36s} {np.median(all_err):7.1f} {np.percentile(all_err, 90):7.1f} {all_err.max():7.1f}")


if __name__ == "__main__":
    main()
