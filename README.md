# 🚀 Houston

**A desk robot that answers questions by pointing at things with a laser.**

Built at **UB Hacking 2026** (Nov 7–8, 24 hours, space theme).

> "Houston, where do I plug the red probe on the multimeter?"
>
> *Houston explains out loud while a red laser dot walks from jack to jack.*

Houston watches a desk through a webcam, listens for its name, and answers by talking **and** pointing a laser at the answer. Other assistants can only talk. Houston can show you.

---

## What it can do

| You say | Houston does |
| --- | --- |
| "Houston, find the soldering iron" | Boxes it on screen, puts the laser on it, and says where it is |
| "Houston, where do I plug the red probe on the multimeter?" | Says a short answer, then points at each jack in order while explaining it |
| "Houston, how many Red Bulls are on the table?" | Answers, then points at each can separately |
| "Houston, what can I build with this?" | Suggests a project and points at the parts it would use |
| "Houston, find the wire strippers" (out of the laser's reach) | Says they're outside its reach, or points at the part it can reach |

Directions are given from **your** side of the table: the person sits across from the camera, facing it.

---

## How it works

1. **Wake word:** Vosk listens offline for "Houston" only.
2. **Speech to text:** after the wake word, Houston records until you stop talking, then Whisper (`base.en`, local) transcribes the request.
3. **Routing:** a request is either a **find** ("find the pliers", "where are my keys") or a **question** ("how many…", "where do I plug…", "what can I build…").
4. **Vision:** Gemini looks at the current camera frame. Finds return a box. Questions return a spoken answer plus up to 6 pointing steps, and each step gets a zoom-in second look so it lands on the exact spot.
5. **Aiming:** a calibrated model of the pan-tilt mount turns the target pixel into servo pulses. The camera then checks where the dot actually landed and corrects it (up to 3 tries).
6. **Voice:** ElevenLabs speaks the answer; during a pointing tour, each step is spoken as the laser reaches it.

The Arduino only moves servos. It clamps tilt so the laser can never point up, and a 300 ms watchdog stops it if the laptop goes quiet. All the math runs on the laptop.

---

## Hardware

| Part | Notes |
| --- | --- |
| Arduino Uno | Runs `firmware/` |
| 2 × MG90S servos on a pan-tilt mount | Pan on **pin 8**, tilt on **pin 7** |
| 1 mW Class 2 red laser | On the tilt servo |
| Logitech Brio 100 webcam | 1280×720 at 30 fps, looking down at the desk |
| USB speaker | For Houston's voice |
| Separate 5 V supply for the servos | Grounds tied to the Uno. Without it the Uno browns out and drops off USB when the servos move |

---

## Setup

```bash
# 1. Python environment
uv venv .venv
uv pip install -p .venv -r requirements.txt
.venv/bin/python -m app.download_voice_model      # Vosk wake-word model (~40 MB)

# 2. API keys: create .env (never committed)
cat > .env <<'EOF'
GEMINI_API_KEY=your-gemini-key
ELEVENLABS_API_KEY=your-elevenlabs-key
EOF

# 3. Firmware (PlatformIO)
cd firmware && pio run -t upload && cd ..

# 4. Run (use the Brio's number from `v4l2-ctl --list-devices`)
set -a && . ./.env && set +a
.venv/bin/python main.py --camera 5
```

Whisper downloads its model (~150 MB) the first time it runs.

On Linux, your user must be able to open the Uno's serial port (on Arch: `sudo usermod -aG uucp $USER`, then log out and back in).

`main.py` options: `--camera N` picks the webcam, `--port /dev/ttyACM0` picks the Arduino port (auto-detected by default), and `--no-laser` runs without the Arduino.

---

## Calibrating the laser

Calibrate once per mount position. It's saved to `data/aim_calibration.json` and loads automatically.

1. Click the video so the keyboard goes to the laser (not the chat box).
2. Optional: press **M**, move the dot to one corner of the area you care about and press **1**, then to the opposite corner and press **2**. Calibration then stays inside that box. Press **M** again to leave move mode.
3. Press **K**. The laser steps through a grid. **Click the red dot** each time, or press **S** if you can't see it. **Backspace** redoes the last point, **Enter** finishes early (8 clicks minimum), **Esc** cancels.
4. When it finishes, the reachable area is outlined in blue. Click anywhere inside it to test.

The fit also measures the mount's build errors (laser and tilt axis a few degrees off square). A bad fit is refused, and the previous calibration stays in use.

## Controls

| Key / mouse | Does |
| --- | --- |
| Click on the video | Aim the laser at that spot |
| Right-click | "The dot actually landed here": adds a calibration point and re-aims |
| **K** | Calibrate |
| **M** | Move mode: **A/D** pan, **W/S** tilt, **[ ]** step size, **C** center, **1/2** mark the calibration area, **0** clear it |
| **Esc** | Leave calibration or move mode (closes the app otherwise) |
| Chat box | Type a find or a question instead of speaking |

## Settings (environment variables)

| Variable | Default | What it does |
| --- | --- | --- |
| `HOUSTON_MIRROR` | `1` | Directions from the user's side of the table. `0` = camera's side |
| `GEMINI_FIND_MODEL` | `gemini-3.5-flash` | Model for finds |
| `GEMINI_ANSWER_MODEL` | `gemini-3.5-flash` | Model for questions with pointing |
| `HOUSTON_WHISPER_MODEL` | `base.en` | Whisper model size (`small.en` is slower, sometimes more accurate) |
| `HOUSTON_MIC_DEVICE` | system default | Microphone number |
| `HOUSTON_AUDIO_OUT` | PipeWire / Pulse | Speaker device name or number |
| `HOUSTON_VOICE_DEBUG` | off | `1` prints what the wake word and Whisper heard |

---

## Repo layout

```
firmware/                 Arduino firmware (PlatformIO): servos, tilt clamp, watchdog
main.py                   Starts everything
app/serial_link.py        Talks to the Uno: heartbeat, auto-reconnect
app/aim.py                Pan-tilt model, calibration fit, aiming, camera check, reach area
app/laser_controller.py   Calibration, move mode, click-to-aim, pointing tours
app/analyzer.py           Gemini: finds, answers, pointing steps
app/voice_listener.py     Wake word (Vosk) + speech to text (Whisper)
app/speaker.py            ElevenLabs voice
app/tk_camera_app.py      The mission control window
tools/aim_sim.py          Simulator used to test every aiming change
tools/bakeoff.py          Early model bake-off scripts
```

## Testing the aiming without hardware

```bash
.venv/bin/python tools/aim_sim.py                       # accuracy across random simulated mounts
.venv/bin/python tools/aim_sim.py --camera-check        # camera check on rendered frames
```

## Safety

- 1 mW Class 2 laser only.
- The firmware clamps tilt so the beam can't be aimed above the desk, whatever the laptop sends. Set `TILT_MIN_US` / `TILT_MAX_US` in `firmware/src/main.cpp` for your mount.
- Keep the laser away from eyes. In this build it is always powered; wiring it through the MOSFET on its own pin would let the watchdog switch it off.
