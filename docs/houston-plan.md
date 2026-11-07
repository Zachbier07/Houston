# Houston: Our UB Hacking 2026 Project

Nov 7, 2026 · @Gio · Rev 2

## The idea

Houston is a robot that points at stuff. You ask it a question out loud, and a laser dot lands on the answer.

- **Theme fit:** this year's theme is space. "Houston, where are my keys?" Our setup looks like mission control.
- **Who it's for:** people working at a messy desk or lab bench.
- **One-line pitch:** "Other AI assistants can only talk. Houston can show you."

## What it does

- **Find:** "Where are my AirPods?" The dot lands on them.
- **Memory:** if something's gone, it tells you where it last saw it and shows you the photo.
- **Guide:** "How do I use this multimeter?" It points at each knob and port, one step at a time.
- **Check:** "Check my build." The dot lands on the piece that's wrong.
- **Notice:** "What changed since I left?" The dot jumps to everything that moved.
- **Mission control screen:** shows what Houston sees and remembers, plus a live scoreboard of how often it gets it right.

## The demo (2 minutes)

1. Judge hides their own keys or AirPods on our messy desk.
2. They ask Houston where it is. The dot lands on it.
3. They ask how to use the multimeter. Houston points at each step.
4. We show the screen with how often it gets it right.

If it misses, we say so and show it fixing itself.

## How it works

1. A camera watches the desk and saves a snapshot every 30 seconds.
2. You say "Hey Houston" and ask a question.
3. One AI call gets the camera frame plus what you said. It sends back what to do, where to point and what to say.
4. A laser on two servos points at it.
5. The camera sees the dot and nudges it until it's right on target.

Step 5 is what makes it accurate, and it's the part judges will love.

**Build order:** hardware, calibration and Find first, then Memory and Guide, then the screen, then Check and Notice. That way we always have something working to show. Hour-by-hour plan is in houston-build-plan.md.

## Tech stack

Full spec in houston-stack.md. The brain model gets picked by a bake-off in the first few hours.

| Part | What we use |
| --- | --- |
| Laser + servos | Arduino Uno, C++ (PlatformIO), talks to the laptop over USB serial |
| Main program | Python 3.11 on a laptop, one asyncio app |
| Camera, calibration, aiming, Notice | OpenCV |
| Brain: understanding + finding + what to say | One multimodal model: Gemini Robotics-ER 2, Claude Haiku 5.5 or Claude Sonnet 5.5, whichever wins the bake-off |
| Memory | SQLite + desk snapshots |
| Voice in | openWakeWord ("Hey Houston") + Silero VAD + faster-whisper |
| Voice out | ElevenLabs Flash, Piper backup, pre-recorded ack clips |
| Mission control screen | Vite + React, WebSocket to the Python app |

## OpenCV vs. the brain: who does what

OpenCV handles anything that has to be instant. The brain gets called once per question, when Houston actually has to think.

|  | OpenCV (on the laptop) | Brain (cloud) |
| --- | --- | --- |
| Speed | Real time, 30+ frames a second | 1-2.5 seconds per question |
| Cost | Free | Pay per call, needs Wi-Fi |
| Good at | Finding the laser dot, calibration, spotting what moved | Understanding a question, recognizing anything on the desk, reading a multimeter, judging a build |
| Bad at | Anything that needs understanding | Speed, and sometimes being confidently wrong |

**In Houston:**

- **OpenCV:** camera-to-laser calibration, finding the dot so we can correct the aim, comparing frames for Notice
- **Brain:** what you asked, where the thing is, the Guide steps, the mistake in a build, and the line Houston says

This is our answer when judges ask "is this just a GPT call?" The model says *where*. Getting a laser to land exactly there on any table, every time, is the aiming loop, and that's all ours.

## The physical setup

Everything runs on a laptop kept out of sight. Houston works on any table we get. No desk mat, so it has to handle bare tables.

- **One base unit:** camera, pan-tilt and laser on the head of a desk lamp. The lamp also lights the desk evenly.
- **Fixed camera-to-laser geometry:** camera and pan-tilt are locked to each other, so moving the unit doesn't break the aim math.
- **Quick recalibration:** one button, laser sweeps, Houston re-learns the table and sets its dot threshold for that surface. Under 1 minute.
- **Bare table:** camera exposure locked low so the dot stands out. The on/off diff ignores table color. Avoid glossy or dark tables when we have a choice.
- **Laptop out of sight:** under the table or in a bag.
- **Bring our own mess:** a bin of props (multimeter, LEGO, desk clutter) so the demo desk looks the same everywhere.

**What runs where:**

- **Arduino:** moves the servos, turns the laser on and off, blocks the laser from ever tilting up, shuts it off if the laptop stops talking to it
- **Laptop (hidden):** OpenCV, aiming correction, wake word, speech to text, memory, mission control screen
- **Cloud:** the brain, ElevenLabs voice

**Rules for this setup:**

- Tell judges it runs on a laptop before they ask.
- **The phone hotspot is mandatory.** The brain is cloud-only, so no internet means no Find. Piper keeps the voice working if ElevenLabs dies.

## Parts

Wiring, pin map and power rules are in houston-parts.md.

## Who does what

- **Laser + aiming (Gio):** firmware, serial, calibration, closed-loop aiming, Notice
- **AI vision:** camera, the brain call, memory
- **Voice + executor:** talking to Houston, running what the brain decides, the server
- **Build + setup:** the lamp unit, the mission control screen, the simulator, props, demo script, Devpost

## Why it can win

Last year, 1st place was a polished video game ([Chernov Incident](https://devpost.com/software/chernov-incident-game)) and the best hardware was a robot that drags a vacuum ([DUSTR](https://devpost.com/software/dustr)). The bar is beatable.

- **If Houston works perfectly:** 1st place.
- **If one demo messes up:** 2nd place.

So we build all of it, but only show judges the features that work every single time.

## Watch out for

- **Lab stuff:** all code written at the event. Nothing from the DRONES Lab.
- **Slow answers:** 3-4 seconds, not 10. The ack clip covers the brain call.
- **Confidently wrong brain:** test on the real props, keep score, fix the prompt.
- **Laser safety:** 1 mW Class 2 laser, and firmware never lets it point up.
- **Tiny objects:** use stuff big enough for the camera to see.
- **New table, new lighting:** practice setup and recalibration on at least 3 different tables before judging.
- **Rate limits:** check the API tier before rehearsals.
