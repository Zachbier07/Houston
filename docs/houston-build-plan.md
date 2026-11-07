# Houston: Event Build Plan

Nov 7, 2026 · @Gio · Rev 2

Times assume hacking started at **12:00 PM Saturday** (H0) and judging is around **12:00 PM Sunday** (H24). The H-numbers line up with `houston-milestones.md`.

## How we work

- **Four tracks run in parallel.** Each person owns their folders and only calls other tracks through the interfaces in `CLAUDE.md`.
- **Skeleton first.** Stub modules with the agreed function names, so everyone builds against them right away.
- **Simulator.** `main.py --sim` swaps in a fake serial port and a fake camera, so nobody waits on hardware.
- **Git:** small commits, push to `main` often, pull before you start. Only touch files in your own folders.
- **Claude Code:** one milestone per prompt. Always say "read docs/houston-stack.md section N first." Test on real hardware before you commit.
- **Save points:** `git tag h10`, `h16`, `h20`, only when it works.

## Who owns what

| Track | Owner | Folders |
| --- | --- | --- |
| Laser + aiming | Gio | `firmware/`, `app/serial_link.py`, `app/aim.py`, `app/notice.py` |
| AI vision | TBD | `app/camera.py`, `app/brain.py`, `app/memory.py`, `tools/bakeoff.py` |
| Voice + executor | TBD | `app/voice/`, `app/main.py`, `app/server.py` |
| Build + setup | TBD | Hardware, `screen/`, `app/sim.py`, `tests/`, props, demo script, Devpost |

## Step 0: Skeleton + simulator (now, ~30 min)

> Read CLAUDE.md and docs/houston-stack.md. Create a Python 3.11 uv project in app/ with stub modules for serial_link, camera, aim, brain, memory, notice, server, sim, and a voice/ package. Use the exact interface names from CLAUDE.md. Stubs return fake values and log that they're stubs. main.py starts one asyncio app and takes a --sim flag. No real logic yet.

## Hour-by-hour

### H0-H4 (12-4 PM): Every piece alive on its own, brain picked

| Track | Build | Done when |
| --- | --- | --- |
| Build | Mount the camera and pan-tilt on the lamp. Solder the servo power rail, wire the laser MOSFET, run the continuity checks. **Gio needs wired hardware ASAP.** Then `sim.py` (stack doc section 15). | Checklist in houston-parts.md passes. `--sim` shows a fake dot moving. |
| Gio | Firmware + Python serial class. Then the on/off dot finder. | **H2:** typed command moves the laser, pulling USB kills the laser, high tilt gets clamped. **H4:** dot position prints live on the table. |
| AI vision | `camera.py` (low locked exposure). Shoot the 15 bake-off photos on the real table. Run the bake-off. | **H4:** brain model picked and written in the stack doc |
| Voice | Wake word ("hey jarvis") → VAD → faster-whisper, printing text | A question from 1 m away prints the right text in under 1.5 s |

### H4-H10 (4-10 PM): MVP = voice Find

| Track | Build | Done when |
| --- | --- | --- |
| Gio | **H6:** 5×5 calibration with auto dot threshold, saved to file. **H8:** `point_at` closed-loop correction. | Click a spot in the camera window and the dot lands within ~1 cm, 9/10 |
| AI vision | `brain.py`: `ask(frame, text, history)` → parsed JSON. Coordinate conversion to pixels. Retry on bad JSON. | Typed "where are my keys" returns the right pixel on live frames |
| Voice | Voice out (ack clips, ElevenLabs, Piper fallback). Executor in `main.py`: runs `find` and `speak`. | Houston speaks the `say` line; pull the Wi-Fi and Piper takes over |
| Build | `tests/trial_runner.py` (N tries → hit/miss CSV). Lock the lamp joints, weigh the base. Start the screen against fake WebSocket data. | Can run 20 Finds and get a score |

**H8 integration (8 PM, 30 min, Gio + AI vision):** typed text → `ask` → `point_at`.

**H10 MVP (10 PM):** "Hey Houston, where are my keys?" → dot lands → it answers out loud. Run 20 tries and time them. **`git tag h10`.**

> If H10 slips, everyone stops their own track and helps finish Find. Nothing else matters until it works.

### H10-H16 (10 PM-4 AM): Memory, Guide, screen

| Track | Build | Done when |
| --- | --- | --- |
| AI vision | **H14:** Memory: 30 s snapshots, Find log, `where_last` with the snapshot fallback. Guide prompt for the multimeter. | 9/10 Memory, 9/10 full Guide walkthroughs |
| Voice | Executor runs `where_last` and `guide` (waits for "next"). FastAPI + WebSocket + `broadcast`. | Guide runs end to end by voice |
| Gio | Aim reliability: tune tolerance, handle a lost dot, log misses. One-button recalibration. Help Guide point at small targets. | Find 18/20. Recalibration under 1 min. |
| Build | Mission control screen on real data | **H16:** screen updates live during a real Find |

**`git tag h16`** once the screen works with real data.

### H16-H20 (4-8 AM): Gate, then extras

- **H18 gate:** run Find 20 times. **18/20 or better:** start Check (AI vision, brain prompt) and Notice (Gio, OpenCV diff). **Under 18:** everyone works on reliability.
- Build track: demo script, Devpost draft, pitch.
- **H20 (8 AM): feature freeze. `git tag h20`.** Only bug fixes after this.

### H20-H24 (8 AM-noon): Rehearse

| Time | Do |
| --- | --- |
| 8-9 AM | Fill in the scoreboard with real rehearsal numbers. Cut anything under its pass rate from the demo. |
| 9-10 AM | Full demo 3× **on a different table**: teardown, setup, recalibration, 2-minute run |
| 10-11 AM | Record the demo video. Finish Devpost. |
| 11-noon | One last dry run, then hands off. No code changes. Charge everything. |

## Sleep

| Shift | Sleeps | Awake |
| --- | --- | --- |
| 1-4 AM | Build + Voice | Gio + AI vision |
| 4-7 AM | Gio + AI vision | Build + Voice |

Nobody sleeps before the H10 MVP works.

## Things that go wrong, and what to do

| Problem | Do this |
| --- | --- |
| Servos jitter or the Uno resets | Check the shared ground and that the adapter isn't on the Uno's 5V pin |
| Can't find the dot | Lower the exposure, check it's really locked, dim the lamp |
| Glare spot on a glossy table | Tilt the lamp head so the reflection moves off the target area |
| Calibration drifts | A lamp joint moved. Tape it and recalibrate. |
| Brain points at the wrong thing | Check it against the bake-off photos. Fix the prompt, not the plan. |
| Brain is slow | Smaller frame, shorter prompt, faster model |
| Wake word fires on chatter | Raise the threshold, test in the actual room |
| Wi-Fi dies | Phone hotspot. The brain is cloud-only, so no hotspot = no Find. |
| Rate limited | Check the API tier now. Rehearsals burn calls fast. |
| Something broke after a tag | `git checkout h16` (or whichever), don't debug at 11 AM |
