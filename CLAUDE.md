# Houston

Desk robot for UB Hacking 2026 (Nov 7-8, 24 hours, space theme). You ask out loud ("Hey Houston, where are my keys?") and a laser dot on a pan-tilt lands on the answer. Goal: 1st place overall.

Full specs live in `docs/`:
- `docs/houston-stack.md`: modules, interfaces, the brain's JSON format, serial protocol, latency budget. Read the section for a module before building it.
- `docs/houston-build-plan.md`: who owns what and what to build each hour.
- `docs/houston-milestones.md`: milestones and the pass rates each demo feature needs.
- `docs/houston-parts.md`: wiring, pin map, power rules.

## Rules

- All code gets written at the event. Don't reuse code from the DRONES Lab or other projects.
- Reliability beats features. Build in milestone order. Don't add scope unless asked. Feature freeze is at H20.
- Answers should land in 3-4 s. One brain call per question. Everything else (aiming, dot finding, Notice) runs locally.
- Laser safety is enforced in firmware, not in Python: tilt clamp (the laser can never aim above the desk), a 300 ms watchdog that turns the laser off, and a boot state of laser off with servos centered.
- No desk mat. Houston has to work on bare tables.
- Keep code simple and readable. Four people are editing fast.
- Mark clearly whether code runs on the Arduino or on the laptop.

## Stack (locked)

| Part | Pick | Runs on |
| --- | --- | --- |
| Firmware | C++, PlatformIO, Arduino framework, Servo lib | Arduino Uno |
| Serial | pyserial, 115200 baud | Laptop ↔ Uno |
| Main app | Python 3.11, one asyncio process, uv | Laptop |
| Camera, aiming, Notice | OpenCV + NumPy | Laptop |
| Brain | One multimodal call: frame + transcript → JSON `{action, point, say, ...}`. Model picked by bake-off (see stack doc). | Cloud |
| Memory | SQLite (`data/houston.db`) + snapshots every 30 s (`data/snaps/`) | Laptop |
| Voice in | openWakeWord ("Hey Houston", "hey jarvis" for dev), Silero VAD, faster-whisper small.en int8 | Laptop |
| Voice out | ElevenLabs Flash streaming, Piper fallback, pre-recorded ack clips | Cloud / laptop |
| Server | FastAPI + one WebSocket, `broadcast(type, data)` | Laptop |
| Screen | Vite + React, served by FastAPI | Browser |

No YOLO and no regex router. The brain does all understanding and recognition.

## Serial protocol (laptop → Uno)

| Command | Meaning |
| --- | --- |
| `A <pan_us> <tilt_us>` | Aim (servo pulse widths in microseconds) |
| `L 1` / `L 0` | Laser on / off |
| `H` | Heartbeat, every 100 ms |

Uno replies `OK` or `E <reason>`. The Uno resets when the port opens, so wait about 2 s before sending anything.

## Hardware pins (Uno)

D9 pan servo, D10 tilt servo, D7 MOSFET gate (laser, low-side 2N7000). Laser runs on 3.3V. Servos run on a separate 5 V 3 A adapter with a shared ground. Nothing connects to the Uno's 5V pin.

## Module boundaries

Other modules only call these. Don't reach around them.
- Serial: `aim_to(pan_us, tilt_us)`, `laser(on)`
- Camera: `latest_frame()` (background thread, exposure/focus/WB locked, exposure low)
- Aiming: `point_at(x, y)` returns hit/miss and the error in pixels. Uses a laser on/off frame diff to find the dot, a 5×5 calibration fit with a 2nd-order polynomial saved to a file, and closed-loop correction (up to 5 tries).
- Brain: `ask(frame, text, history)` returns the parsed JSON dict. Points come back as `[y, x]` 0-1000 and get converted to pixels inside `brain.py`.
- Memory: `save_snapshot()`, `log_find(object, x, y)`, `where_last(object)`
- Notice: `changes_since(snapshot)` returns a list of pixel centers
- Server: `broadcast(type, data)` with types `frame`, `state`, `transcript`, `memory`, `score`
- Sim: `main.py --sim` swaps in `FakeSerial` and `FakeCamera`

## Repo layout

```
firmware/     PlatformIO project
app/          Python: serial_link, camera, aim, brain, memory, notice, voice/, server, sim, main
screen/       Vite + React
tools/        bakeoff.py and other throwaway scripts
tests/        trial_runner.py
assets/acks/  ack clips
assets/wake/  wake word model
data/         db, snapshots, bake-off photos (gitignored)
.env          API keys (gitignored)
```
