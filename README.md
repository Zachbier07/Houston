# 🚀 Houston

**A desk robot that answers questions by pointing at things with a laser.**

Built at **UB Hacking 2026** (Nov 7–8, 24 hours, space theme).

> "Hey Houston, where are my keys?"
>
> *A red laser dot moves across the desk and lands on your keys.* "Copy that. Your keys are next to the laptop."

Houston sits on a desk lamp and watches your desk through a camera. You talk to it, and it answers by speaking and by pointing a laser at the right spot. It works on any bare table, and the goal is an answer in 3 to 4 seconds.

---

## What it can do

| Feature | What you say | What Houston does |
| --- | --- | --- |
| **Find** | "Where are my AirPods?" | Picks them out in the camera view and puts the laser dot on them. |
| **Memory** | "Where did I leave my multimeter?" (after it's gone) | Looks back through its desk snapshots, says where it last saw it and shows the photo on screen. |
| **Guide** | "How do I use this multimeter?" | Points at each knob or port in order and waits for you to say "next." |
| **Check** | "Is my build right?" | Finds the mistake (for example a LEGO piece in the wrong spot) and points at it. |
| **Notice** | "What changed?" | Compares the desk to an earlier snapshot and points at each spot that changed. |

A **mission control screen** shows the live camera view, what Houston heard and said, its memory log, and a live accuracy scoreboard (hits per try for each feature).

---

## How it works

```
  You talk ──► "Hey Houston" ──► records until you stop ──► speech to text
                                                                 │
  Camera ──► latest photo of the desk ──────────────────────────►│
                                                                 ▼
                                      The brain: ONE AI call gets the photo + your words
                                      and sends back what to do, where to point, what to say
                                                                 │
                                                                 ▼
             Laptop works out the servo angles ──USB──► Arduino ──► laser moves
                                                                 │
                                                                 ▼
                              Houston answers out loud + screen updates
```

In plain words:

1. **Listening.** The laptop always listens for "Hey Houston." When it hears that, it records until you stop talking and turns your speech into text, all on the laptop.
2. **Thinking.** Houston sends one request to a multimodal AI model in the cloud: the latest camera photo plus what you said. The model answers with a short instruction like "find, point here, say *Right there, by the notebook*." Houston plays a quick "Copy that" right away so you're not left waiting in silence.
3. **Pointing.** The laptop converts that spot on the photo into servo angles and sends them to an Arduino, which moves a small pan-tilt mount holding the laser. The dot starts moving before Houston starts talking.
4. **Correcting.** The camera finds where the dot actually landed and nudges it until it's on target (up to 5 tries). That's what makes it accurate.
5. **Answering.** Houston speaks the answer, and the mission control screen updates.

### Who does what: OpenCV vs. the AI

| | OpenCV (on the laptop) | The brain (cloud AI) |
| --- | --- | --- |
| Speed | Real time, 30+ frames a second | 1 to 2.5 seconds per question |
| Used for | Finding the laser dot, calibration, spotting what moved (Notice) | Understanding the question, recognizing anything on the desk, reading a multimeter, judging a build |

So it's not just an AI call. The model says *where*. Getting a laser to land exactly there on any table, every time, is the aiming loop, and that part is all ours.

### Memory

The camera saves a snapshot of the desk every 30 seconds and after every question. When you ask where something went, Houston first checks the places it already found that thing. If that doesn't answer it, it asks the brain to look through the last few snapshots.

### Calibration

Houston has to learn how servo angles map to spots on the desk. One button on the screen makes the laser sweep a 5 × 5 grid while the camera watches the dot. The same sweep also tunes how bright the dot looks on that table. It takes under a minute and gets saved, so moving to a new table only means one quick recalibration.

### No desk mat

Houston runs on whatever table we get. The camera's exposure is locked low so the laser dot is the brightest thing in view, and the dot finder compares a photo with the laser off to one with it on, so the table's color doesn't matter.

---

## The hardware

- **Arduino Uno** for the servos and laser switch
- **2 × MG90S servos** on a 3D-printed pan-tilt mount
- **1 mW red laser** (Class 2, the eye-safe kind used in laser pointers), switched by a MOSFET
- **USB webcam** mounted on a **desk lamp** looking down. It's also the microphone.
- **USB speaker** for Houston's voice
- **5 V 3 A adapter** that powers only the servos
- **A laptop** that runs everything else

### Safety

Laser safety is handled on the Arduino, so a bug in the laptop code can't get around it:

- **The laser can never point above the desk.** Tilt is clamped in firmware.
- **The laser shuts off on its own** if the laptop goes quiet for 300 ms.
- **It starts safe.** On power-up the laser is off and the servos are centered.

---

## The software

| Piece | What we use | Runs on |
| --- | --- | --- |
| Firmware | C++ (PlatformIO, Arduino) | Arduino Uno |
| Main app | Python 3.11 | Laptop |
| Camera, aiming, Notice | OpenCV, NumPy | Laptop |
| Brain | One multimodal model: Gemini Robotics-ER 2, Claude Haiku 5.5 or Claude Sonnet 5.5, picked by a bake-off in the first hours | Cloud |
| Wake word | openWakeWord ("Hey Houston") | Laptop |
| Speech to text | faster-whisper | Laptop |
| Voice | ElevenLabs (offline backup: Piper) | Cloud / laptop |
| Memory | SQLite + desk snapshots every 30 s | Laptop |
| Mission control screen | FastAPI + React | Browser |

Everything fast runs on the laptop. Each question makes exactly one call to the brain, so Houston needs internet: the phone hotspot is mandatory.

A **simulator** (`main.py --sim`) swaps in a fake Arduino and a fake camera, so the team can build without waiting on hardware.

---

## Repo layout

```
firmware/     Arduino code (servos, laser, safety)
app/          Python app (serial link, camera, aiming, brain, memory, Notice, voice, server, simulator)
screen/       Mission control screen (React)
tools/        Throwaway scripts (like the brain bake-off)
tests/        Trial runner that scores hits and misses
assets/       Voice clips and the wake word model
docs/         Full specs: plan, stack, build plan, milestones, parts
data/         Memory database + snapshots (not committed)
```

---

## Build plan

All code is written during the 24 hours of the event. Each milestone leaves us with something we could demo if we ran out of time.

| Hour | Milestone |
| --- | --- |
| H2 | Laptop moves the laser over USB. Safety limits work. |
| H4 | Brain model picked. Camera finds the laser dot on the table. |
| H6 | One-button calibration works. |
| H8 | Type "where are my keys" and the dot lands on them. |
| H10 | **MVP:** say it out loud and Houston points and answers. |
| H14 | Memory + Guide |
| H16 | Mission control screen |
| H18 | Check + Notice, only if Find is solid (18/20) |
| H20 | Feature freeze, bug fixes only |
| H22 | Full demo rehearsed 3 times |
| H24 | Judging |

A feature only goes in the demo if it passes in rehearsal (Find has to hit 18 out of 20). The hour-by-hour plan, with who builds what, is in [`docs/houston-build-plan.md`](docs/houston-build-plan.md).

---

## Running it

*Setup instructions will go here once the code is written at the event.*
