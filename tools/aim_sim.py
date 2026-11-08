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
import sys
from pathlib import Path

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
        # camera: 0.75 m up, behind the laser, looking down at the table
        self.cam = np.array([0.0, -0.45, 0.75])
        self.cam_R = look_at(self.cam, np.array([0.0, 0.2, 0.0]))       # world -> camera
        self.f = 950.0
        self.k1 = -0.06

    def dot_world(self, pan, tilt):
        theta = np.radians((pan - aim.CENTER_US[0]) / self.k_pan)
        phi = np.radians((tilt - aim.CENTER_US[1]) / self.k_tilt + self.tilt0)
        d_local = np.array([np.cos(phi) * np.sin(theta), np.cos(phi) * np.cos(theta), np.sin(phi)])
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
    args = parser.parse_args()

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
