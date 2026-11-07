# Houston: Final Code Stack

Oct 21, 2026 · @Gio

This locks the stack. The only open pick is the VLM (see the bottom of this file).

## The stack at a glance

| Part | Pick | Runs on |
| --- | --- | --- |
| Firmware | C++, PlatformIO, Arduino framework, Servo library | Arduino Uno |
| Serial link | pyserial, USB, 115200 baud | Laptop ↔ Arduino |
| Main app | Python 3.11, one asyncio process, deps managed with uv | Laptop |
| Camera | OpenCV | Laptop |
| Aiming | OpenCV + NumPy | Laptop |
| Fast object finding | Ultralytics YOLO-World (test YOLOE at M2) | Laptop |
| Pointing on hard targets | VLM: Gemini Robotics-ER 2 vs. Claude | Cloud |
| Router | Fast path + fast LLM with tool calls | Laptop + cloud |
| Memory | SQLite + snapshot JPEGs | Laptop |
| Wake word | openWakeWord, custom "Hey Houston" model | Laptop |
| End of speech | Silero VAD | Laptop |
| Speech to text | faster-whisper (small.en, int8, CPU) | Laptop |
| Voice out | ElevenLabs Flash (streaming), Piper backup, ack clips | Cloud / laptop |
| Server | FastAPI + one WebSocket | Laptop |
| Mission control screen | Vite + React | Browser on the laptop |

## How it fits together

```
 Mic ──► wake word ──► VAD ──► faster-whisper ──► Router
                                                    │
         ┌─────────────── fast path ────────────────┤
         ▼                                          ▼
     YOLO-World ◄── Camera ──► Memory          LLM tools ──► VLM
         │                                          │
         └──────────► target pixel ◄────────────────┘
                           │
                         Aim ──serial──► Arduino ──► servos + laser
                           │
                     Voice out + WebSocket ──► Mission control screen
```

## What each part does

### 1. Arduino firmware (Gio)

- **Takes in:** text commands over USB serial from the laptop.
- **Does:**
  - Moves the pan and tilt servos to the pulse widths it's told (in microseconds, for finer steps than whole degrees).
  - Turns the laser on and off through a MOSFET.
  - Clamps tilt so the laser can never aim above the desk, no matter what the laptop sends.
  - Watchdog: no heartbeat for 300 ms means laser off.
  - Boots with the laser off and servos centered.
- **Sends back:** `OK` after each command, `E <reason>` on errors (watchdog tripped, tilt clamped).
- **Doesn't do:** any math. It's a dumb, safe muscle. All thinking happens on the laptop.

### 2. Serial link (Gio)

- **Does:** one Python class that owns the USB port. Sends aim and laser commands, sends a heartbeat every 100 ms in the background, reconnects if the cable gets bumped.
- **Everyone else** calls `aim_to(pan_us, tilt_us)` and `laser(on)` and never touches the port directly.

| Laptop → Arduino | Meaning |
| --- | --- |
| `A <pan_us> <tilt_us>` | Aim |
| `L 1` / `L 0` | Laser on / off |
| `H` | Heartbeat |

### 3. Camera (AI vision)

- **Does:** runs in its own thread, grabs frames nonstop, and keeps the latest one ready so nobody waits on the camera.
- **At startup:** locks exposure, focus and white balance so lighting changes don't wreck dot finding or detection.
- **Gives others:** `latest_frame()`.

### 4. Aiming (Gio)

The part that makes Houston accurate. Three jobs:

- **Find the dot:** turn the laser off, grab a frame, turn it on, grab another, diff them. The bright blob that appeared is the dot. Works under any lighting.
- **Calibrate:** sweep the laser over a 5 × 5 grid, find the dot at each point, fit a pixel → servo pulse map (2nd-order polynomial in NumPy). One button, under a minute. Saves to a file so a restart doesn't need a recalibration.
- **Point and correct:** given a target pixel, aim with the map, find where the dot really landed, nudge by the error, repeat until within a few pixels or 5 tries.
- **Gives others:** `point_at(x, y)`, which returns hit or miss and the final error in pixels. The miss feeds the scoreboard.

### 5. Fast object finding: YOLO-World (AI vision)

- **Takes in:** a camera frame and a list of words ("keys", "AirPods", "multimeter").
- **Does:** finds those objects and returns boxes with confidence scores, in well under 0.1 s.
- **Runs two ways:**
  - **Background:** every second or so on a fixed list of desk objects, writing sightings to Memory.
  - **On demand:** when someone asks for something, with that exact word.
- **Gives others:** the center pixel of the best box, or "not found."

### 6. VLM: Gemini Robotics-ER 2 or Claude (AI vision)

Only called when the fast detector can't handle it.

- **Takes in:** one camera frame plus a question.
- **Used for:**
  - **Hard Find:** objects YOLO-World doesn't know or can't pick out ("the blue resistor", "the cable plugged into port 2").
  - **Guide:** reads the multimeter or tool and returns an ordered list of steps, each with a point to aim at.
  - **Check:** looks at a build and returns the point where something's wrong, plus a one-line reason.
- **Gives back:** points in pixel coordinates (converted from whatever scale the model uses) plus short text for Houston to say.

### 7. Router / brain (Voice + screen)

Decides what you asked and runs it.

- **Fast path:** if the text matches "where is my X" or "find the X", skip the LLM and go straight to YOLO-World, then Memory, then the VLM if both miss.
- **Everything else:** send the text to a fast LLM with these tools:

| Tool | What it does |
| --- | --- |
| `find(object)` | YOLO-World → VLM fallback → `point_at` |
| `where_last(object)` | Look up Memory, say where and when, show the snapshot on screen |
| `guide(tool)` | VLM gets the steps, Houston points at each and waits for "next" |
| `check()` | VLM finds the mistake, Houston points at it |
| `notice()` | Diff now vs. the last saved desk snapshot, point at each spot that changed |

- **Also:** plays an ack clip right away on any slow path, and logs hit or miss for the scoreboard.

### 8. Memory (AI vision)

- **Stores:** one row per sighting: object, pixel position, time, path to a snapshot JPEG. Old rows get trimmed so the file stays small.
- **Answers:** "where and when was X last seen" and "what did the desk look like at time T" (for Notice).
- **Lives in:** `data/houston.db` and `data/snaps/`.

### 9. Wake word: openWakeWord (Voice + screen)

- **Listens:** always, on the webcam's built-in mic, for **"Hey Houston"**. Two words false-trigger a lot less than one.
- **Model:** custom "Hey Houston" model in `assets/wake/`. Use the built-in "hey jarvis" model while developing in case the custom one isn't ready.
- **Ignores itself:** muted while Houston is talking and for half a second after, so it can't wake itself up.
- **No echo cancellation:** the webcam mic will hear the speaker, so this mute isn't optional. Point the speaker away from the lamp and keep the volume moderate.
- **Distance:** the mic is up on the lamp head, so people talk a little louder than at a laptop. Turn up mic gain and test in a noisy room.
- **Tuning:** set the trigger threshold in the actual room during setup. Too low and it fires on chatter, too high and judges have to shout.
- **On trigger:** plays a short beep and the screen switches to "listening."

### 10. End of speech: Silero VAD (Voice + screen)

- **Does:** after the wake word, records until there's about 0.6 s of silence, then hands the clip off. Hard cap at 8 s so it never hangs in a noisy room.

### 11. Speech to text: faster-whisper (Voice + screen)

- **Takes in:** the recorded clip.
- **Does:** turns it into text on the laptop CPU in about half a second. A short vocab hint ("Houston, multimeter, AirPods, LEGO") helps it spell our words right.
- **Gives others:** the text, which goes to the Router and shows on screen.

### 12. Voice out: ElevenLabs Flash + Piper + acks (Voice + screen)

- **ElevenLabs Flash:** streams Houston's spoken answers so the first word starts playing fast.
- **Piper:** same job, fully offline. Switches over automatically if ElevenLabs errors or the Wi-Fi dies.
- **Ack clips:** a handful of pre-recorded lines ("Copy that", "Scanning the desk", "Stand by") played instantly while slow stuff runs.

### 13. Server: FastAPI (Voice + screen)

- **Does:** serves the built screen page and keeps one WebSocket open to it. Every other module just calls `broadcast(type, data)`.

| Message `type` | What's in it |
| --- | --- |
| `frame` | Live camera image with boxes and the dot, about 10 per second |
| `state` | idle, listening, thinking, pointing |
| `transcript` | What Houston heard and what it said |
| `memory` | Latest sightings, snapshot for "last seen" |
| `score` | Hits / tries per feature |

### 14. Mission control screen: Vite + React (Voice + screen)

- **Shows:** live camera feed, a big status light (listening / thinking / pointing), the conversation, the memory log, and the accuracy scoreboard.
- **Also has:** a calibrate button for setup. That's the only control on it.
- **Look:** dark NASA-style console, big readable numbers from across a table.

## Latency budget (target about 3 s)

| Step | Budget |
| --- | --- |
| You stop talking → VAD notices | about 0.6 s |
| Speech to text | 0.5-0.8 s |
| Router (fast path / LLM) | 0-1.0 s |
| YOLO-World lookup | under 0.1 s |
| Aim + correction | 0.5-1.0 s |
| First audio out (ElevenLabs Flash) | about 0.3 s |
| **Total, Find** | **about 2-3.5 s** |
| VLM path (Guide, Check, hard targets) | +1-5 s, covered by an ack clip |

Start pointing while the voice reply is still being generated. The dot moving is what judges notice first.

## Repo layout

```
houston/
  firmware/        PlatformIO project (Arduino)
  app/             Python: serial, camera, aim, vision, memory, voice, brain, server
  screen/          Vite + React
  assets/acks/     pre-recorded ack clips
  assets/wake/     "Hey Houston" wake word model
  data/            SQLite db + snapshots (gitignored)
  .env             API keys (gitignored)
```

**VLM pick:** _______ (pointing hits __/10, median latency __ s)
