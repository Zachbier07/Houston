# 🚀 Houston

**A desk robot that answers questions by pointing at things with a laser.**

Built at **UB Hacking 2026** (Nov 7–8, 24 hours, space theme).

> "Hey Houston, where are my keys?"
>
> *A red laser dot moves across the desk and lands on your keys.* "Copy that. Your keys are next to the laptop."

Houston sits on a desk lamp and watches your desk through a camera. You talk to it, and it answers by speaking and by pointing a laser at the right spot. The goal is to get an answer in about 3 seconds.

---

## What it can do

| Feature | What you say | What Houston does |
| --- | --- | --- |
| **Find** | "Where are my AirPods?" | Spots them in the camera feed and puts the laser dot on them. |
| **Memory** | "Where did I leave my multimeter?" (after it's gone) | Says where and when it last saw it and shows the photo on screen. |
| **Guide** | "How do I use this multimeter?" | Points at each knob or port in order and waits for you to say "next." |
| **Check** | "Is my build right?" | Finds the mistake (for example a LEGO piece in the wrong spot) and points at it. |
| **Notice** | "What changed?" | Compares the desk to an earlier photo and points at each spot that changed. |

A **mission control screen** shows the live camera view, what Houston heard and said, its memory log, and a live accuracy scoreboard (hits per try for each feature).

---

## How it works

```
  You talk ──► "Hey Houston" ──► records until you stop ──► speech to text
                                                               │
                                                               ▼
                                                      Brain decides what you want
                                                               │
              ┌────────────────────────────────────────────────┤
              ▼                                                ▼
   Fast object finder (local)                     Vision AI in the cloud
   "keys", "AirPods", ...                         (harder questions only)
              │                                                │
              └──────────────► a spot on the camera image ◄────┘
                                        │
                                        ▼
               Laptop works out the servo angles ──USB──► Arduino ──► laser moves
                                        │
                                        ▼
                    Houston answers out loud + screen updates
```

In plain words:

1. **Listening.** The laptop always listens for "Hey Houston." When it hears that, it records until you stop talking and turns your speech into text, all on the laptop.
2. **Thinking.** Simple questions like "where is my X" go straight to a fast object detector running on the laptop. Harder ones (reading a tool, finding a mistake) go to a vision AI model in the cloud. Houston plays a quick "Stand by" so you're not left waiting in silence.
3. **Pointing.** The laptop knows which pixel on the camera image to hit and converts that into servo angles. It sends those to an Arduino, which moves a small pan-tilt mount holding the laser.
4. **Correcting.** The camera finds where the dot actually landed and nudges it until it's on target (up to 5 tries). That's what makes it accurate.
5. **Answering.** Houston speaks the answer, and the mission control screen updates.

### Calibration

Houston has to learn how servo angles map to spots on the desk. One button on the screen makes the laser sweep a 5 × 5 grid while the camera watches the dot. That takes under a minute and gets saved, so moving to a new table only means one quick recalibration.

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
| Camera + aiming | OpenCV, NumPy | Laptop |
| Fast object finding | YOLO-World | Laptop |
| Hard questions | Vision AI (Gemini Robotics-ER or Claude) | Cloud |
| Wake word | openWakeWord ("Hey Houston") | Laptop |
| Speech to text | faster-whisper | Laptop |
| Voice | ElevenLabs (offline backup: Piper) | Cloud / laptop |
| Memory | SQLite + saved photos | Laptop |
| Mission control screen | FastAPI + React | Browser |

Most of the work runs on the laptop because that's faster. The cloud is only used when the local tools can't answer.

---

## Repo layout

```
firmware/     Arduino code (servos, laser, safety)
app/          Python app (camera, aiming, vision, memory, voice, brain, server)
screen/       Mission control screen (React)
assets/       Voice clips and the wake word model
data/         Memory database + snapshots (not committed)
```

---

## Build plan

All code is written during the 24 hours of the event. Each milestone leaves us with something we could demo if we ran out of time.

| Hour | Milestone |
| --- | --- |
| H2 | Laptop moves the laser over USB. Safety limits work. |
| H4 | Camera finds the laser dot. |
| H6 | One-button calibration works. |
| H8 | Type "keys" and the dot lands on them. |
| H10 | **MVP:** say it out loud and Houston points and answers. |
| H14 | Memory + Guide |
| H16 | Mission control screen |
| H18 | Check + Notice, only if Find is solid (18/20) |
| H20 | Feature freeze, bug fixes only |
| H22 | Full demo rehearsed 3 times |
| H24 | Judging |

A feature only goes in the demo if it passes in rehearsal (Find has to hit 18 out of 20).

---

## Running it

*Setup instructions will go here once the code is written at the event.*
