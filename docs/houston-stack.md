# Houston: Code Stack

Nov 7, 2026 · @Gio · Rev 2

This is the spec. Build each module from its section here.

## What changed (Rev 2)

| Was | Now | Why |
| --- | --- | --- |
| YOLO-World finds objects | **One vision model finds everything** | YOLO couldn't recognize most of what's on a real desk |
| Regex fast path + LLM router + separate VLM | **One multimodal call:** frame + what was said → JSON with action, point and spoken line | One brain, one cloud trip per question, one place to debug |
| Background detector fills Memory | **Memory = desk snapshots every 30 s** | No detector anymore. The vision model reads old snapshots when needed. |
| Desk mat | **Bare table** | No mat. Exposure lock + on/off diff handle any matte surface. |

## The stack at a glance

| Part | Pick | Runs on |
| --- | --- | --- |
| Firmware | C++, PlatformIO, Arduino framework, Servo library | Arduino Uno |
| Serial link | pyserial, USB, 115200 baud | Laptop ↔ Arduino |
| Main app | Python 3.11, one asyncio process, deps managed with uv | Laptop |
| Camera | OpenCV | Laptop |
| Aiming | OpenCV + NumPy | Laptop |
| Brain | **One multimodal model, picked by bake-off:** Gemini Robotics-ER 2, Claude Haiku 5.5 or Claude Sonnet 5.5 | Cloud |
| Memory | SQLite + snapshot JPEGs | Laptop |
| Notice | OpenCV frame diff | Laptop |
| Wake word | openWakeWord, custom "Hey Houston" model ("hey jarvis" for dev) | Laptop |
| End of speech | Silero VAD | Laptop |
| Speech to text | faster-whisper (small.en, int8, CPU) | Laptop |
| Voice out | ElevenLabs Flash (streaming), Piper backup, ack clips | Cloud / laptop |
| Server | FastAPI + one WebSocket | Laptop |
| Mission control screen | Vite + React | Browser on the laptop |

## How it fits together

```
 Mic ──► wake word ──► VAD ──► faster-whisper ──► text
                                                   │
 Camera ──► latest frame ─────────────────────────►│
                                                   ▼
                                     Brain (one multimodal call)
                                     returns {action, point, say, ...}
                                                   │
                                       Executor (main.py) runs it
                       ┌──────────────┬────────────┼─────────────┬──────────────┐
                     find         where_last     guide        check/notice     speak
                       └──────────────┴────────────┼─────────────┘
                                                   ▼
                              point_at(x, y) ──serial──► Arduino ──► servos + laser
                                                   │
                              Voice out + WebSocket ──► Mission control screen
```

## What each part does

### 1. Arduino firmware (Gio)

- **Takes in:** text commands over USB serial from the laptop.
- **Does:**
  - Moves the pan and tilt servos to the pulse widths it's told (microseconds).
  - Turns the laser on and off through the MOSFET on D7.
  - Clamps tilt so the laser can never aim above the desk, no matter what the laptop sends.
  - Watchdog: no heartbeat for 300 ms means laser off.
  - Boots with the laser off and servos centered.
- **Sends back:** `OK` after each command, `E <reason>` on errors (watchdog tripped, tilt clamped).
- **Doesn't do:** any math. All thinking happens on the laptop.

### 2. Serial link (Gio)

- One Python class owns the USB port. Sends aim and laser commands, sends a heartbeat every 100 ms in the background, reconnects if the cable gets bumped. Waits ~2 s after opening the port (the Uno resets).
- Everyone else calls `aim_to(pan_us, tilt_us)` and `laser(on)`.

| Laptop → Arduino | Meaning |
| --- | --- |
| `A <pan_us> <tilt_us>` | Aim |
| `L 1` / `L 0` | Laser on / off |
| `H` | Heartbeat |

### 3. Camera (AI vision)

- Runs in its own thread, grabs frames nonstop, keeps the latest one ready.
- Locks exposure, focus and white balance at startup. Exposure stays **low** so the laser dot is the brightest thing on any table.
- Gives others: `latest_frame()`.

### 4. Aiming (Gio)

- **Find the dot:** laser off → frame, laser on → frame, diff them. The blob that appeared is the dot. Works on any matte table.
- **Calibrate:** sweep a 5 × 5 grid, find the dot at each point, fit a pixel → servo pulse map (2nd-order polynomial in NumPy). Use the 25 dot hits to auto-set the dot brightness threshold for this table. One button, under a minute. Saved to a file.
- **Point and correct:** aim with the map, find where the dot really landed, nudge by the error, repeat until within a few pixels or 5 tries.
- **Gives others:** `point_at(x, y)` → hit or miss + final error in pixels. Misses feed the scoreboard.

### 5. Brain: one multimodal call (AI vision)

The one place Houston thinks. It sees and understands in a single call.

- **Takes in:** the latest frame (shrunk to ~640 px wide), the transcript, and the last 2-3 turns of conversation (so "next" and "what about the other one" work).
- **Returns JSON only:**

```json
{"action": "find", "point": [y, x], "say": "Right there, by the notebook."}
```

| `action` | Extra fields | Executor does |
| --- | --- | --- |
| `find` | `point` (or `null` if not visible) | `point_at`. If `null`, run `where_last`. |
| `where_last` | `object` | Memory lookup (see below), show snapshot, point at the old spot |
| `guide` | `steps`: list of `{point, say}` | Point + speak each step, wait for "next" |
| `check` | `point`, `say` | Point at the mistake, explain in one line |
| `notice` | none | Run the local OpenCV diff, point at each change |
| `speak` | `say` | Just talk |

- **Points:** `[y, x]` scaled 0-1000. Convert to camera pixels before `point_at`. Check the chosen model's docs, since formats differ.
- **Settings:** JSON output mode if the model has one, low or no thinking budget, short system prompt. If the JSON doesn't parse, retry once, then say "Say that again?"
- **Gives others:** `ask(frame, text, history)` → parsed dict.

### 6. Memory (AI vision)

- **Stores:** a desk snapshot every 30 s and after each question. One row per snapshot (time, path), plus one row per successful Find (object, pixel, time, snapshot). Old snapshots get trimmed.
- **`where_last(object)`:**
  1. Check the Find log first. If it was found before, use that spot and snapshot. Instant.
  2. Otherwise send the last ~4 snapshots to the brain: "Was the {object} in any of these? Which one, and where?" Covered by an ack clip.
- **Lives in:** `data/houston.db` and `data/snaps/`.

### 7. Notice (Gio or AI vision)

- Diff the current frame against the snapshot from when the user left (the last snapshot before the gap, or the one saved at the previous question). Blur, threshold, find blobs bigger than a minimum size. Point at the center of each one, in order.
- All local, no cloud call.

### 8. Executor (Voice + screen)

- Lives in `main.py`. Takes the brain's dict and runs it: calls `point_at`, Memory, Notice, and voice out.
- **Starts aiming before the voice starts.** The dot moves first.
- Logs hit or miss per action for the scoreboard.

### 9. Wake word: openWakeWord (Voice + screen)

- Always listening on the webcam's built-in mic for **"Hey Houston"**. Use "hey jarvis" while developing.
- Muted while Houston talks and for half a second after. No echo cancellation, so this isn't optional.
- Mic gain up, threshold tuned in the actual room.
- On trigger: short beep, screen switches to "listening."

### 10. End of speech: Silero VAD (Voice + screen)

- Records until ~0.6 s of silence. Hard cap at 8 s.

### 11. Speech to text: faster-whisper (Voice + screen)

- Clip → text on the laptop CPU in about half a second. Vocab hint: "Houston, multimeter, AirPods, LEGO."

### 12. Voice out (Voice + screen)

- **Ack clip** plays the moment whisper finishes ("Copy that", "Scanning the desk"). Covers the brain call.
- **ElevenLabs Flash** streams the `say` line.
- **Piper** takes over if ElevenLabs errors or the Wi-Fi dies.
- **No Wi-Fi at all:** Piper says "Lost link to mission control." The brain is cloud-only, so Houston can't find things offline. The hotspot is mandatory.

### 13. Server: FastAPI (Voice + screen)

- Serves the built screen and keeps one WebSocket open. Everything else calls `broadcast(type, data)`.

| Message `type` | What's in it |
| --- | --- |
| `frame` | Live camera image with the target and the dot drawn on, ~10 per second |
| `state` | idle, listening, thinking, pointing |
| `transcript` | What Houston heard and what it said |
| `memory` | Latest snapshot, "last seen" snapshot |
| `score` | Hits / tries per feature |

### 14. Mission control screen: Vite + React (Build + setup)

- Live camera feed, a big status light, the conversation, the memory snapshots, the accuracy scoreboard, and a calibrate button. Dark NASA-style console, numbers readable from across a table.

### 15. Simulator (whoever's free)

- `app/sim.py`: `FakeSerial` (same interface as the serial link) and `FakeCamera` (draws a red dot on a desk photo wherever the pulses map to, with a made-up polynomial, noise and ~150 ms lag).
- `main.py --sim` swaps them in, so aiming, Notice and the screen can be built before the hardware works.

## Latency budget (target 3-4 s)

| Step | Budget |
| --- | --- |
| You stop talking → VAD notices | about 0.6 s |
| Speech to text | 0.5-0.8 s |
| Brain call | 1.0-2.5 s (covered by the ack clip) |
| Aim + correction | 0.5-1.0 s |
| First audio out | about 0.3 s, overlaps with aiming |
| **Total, Find** | **about 3-4.5 s** |
| `where_last` with snapshots, `guide` | +1-3 s |

If the median Find goes over 4 s at H10: shrink the frame more, cut the system prompt, try the faster model from the bake-off.

## Picking the brain model (bake-off, do first)

- 15 photos from the real camera on the real table with the real props. Each one gets a question and a box around the right answer.
  - 8 easy Finds, 4 hard Finds ("the red wire", "the COM port"), 3 non-Find ("how do I measure voltage", "check my build")
- Run Gemini Robotics-ER 2, Claude Haiku 5.5 and Claude Sonnet 5.5, twice each, with the same prompt.
- Score: hits (point inside the box), right action, valid JSON, median time.
- **Pass:** 13/15 hits, 15/15 valid JSON and right action, median under 2.5 s.
- **Pick:** most hits wins. Within 1 hit of each other, take the faster one.
- Script: `tools/bakeoff.py` (throwaway). Keep the photos as a regression test for prompt changes.

**Brain pick:** _______ (hits __/15, median __ s)

## Repo layout

```
houston/
  firmware/        PlatformIO project (Arduino)
  app/             Python: serial_link, camera, aim, brain, memory, notice, voice/, server, sim, main
  screen/          Vite + React
  tools/           bakeoff.py and other throwaway scripts
  tests/           trial_runner.py
  assets/acks/     pre-recorded ack clips
  assets/wake/     wake word model
  data/            SQLite db, snapshots, bake-off photos (gitignored)
  .env             API keys (gitignored)
```
