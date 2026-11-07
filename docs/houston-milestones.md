# Houston: Deliverables and Milestones

Oct 21, 2026 · @Gio

Two phases. Before Nov 7 we plan, test and buy. No project code (MLH rules). At the event we build in order, and every milestone leaves us with something we could demo if time ran out.

## Owners

| Area | Owner |
| --- | --- |
| Laser + aiming | Gio |
| AI vision | TBD |
| Voice + screen | TBD |
| Build + setup | TBD |

## Phase 1: Before the event

### M0: Go decision (Fri Oct 23)

- [ ] Team says yes to Houston
- [ ] Owners filled in above
- [ ] Email organizers: is a Class 2 laser okay? How does judging work (judges come to us, or we go to a room)? Monitor and outlet available?
- [ ] Ask organizers whether CAD and 3D-printed parts made before the event are allowed

**Done when:** everyone has a role and the emails are sent.

### M1: Parts ordered (Sun Oct 25)

- [ ] Servos + pan-tilt bracket, Class 2 laser (check the label says under 1 mW), wide-angle USB camera (its mic is our mic), USB speaker, Arduino
- [ ] Phone hotspot plan

**Done when:** everything's ordered or confirmed on hand.

### M2: Speed tests done (Sun Nov 1)

Throwaway test scripts only. Nothing here goes into the project.

| Test | Owner | Pass if |
| --- | --- | --- |
| YOLO-World on the laptop | AI vision | 15+ fps, finds keys/AirPods/multimeter on a messy desk |
| VLM pointing: Gemini vs. Claude | AI vision | Pixel coords land on the target 9/10 times, answer in under 3 s |
| Speech to text (faster-whisper) | Voice | Under 1 s for a short question, works with background noise |
| Voice out: ElevenLabs vs. Piper | Voice | First audio in under 1 s |
| Full chain on paper | Everyone | Wake word → whisper → LLM → detector → aim → voice adds up to about 3 s |

**Done when:** we've picked a VLM and have a written latency budget.

### M3: Ready to build (Thu Nov 5)

- [ ] Base unit designed in Onshape (weighted base or table clamp, camera and pan-tilt locked together)
- [ ] Prop bin packed: multimeter, LEGO build with a planted mistake, desk clutter
- [ ] Fold-up mission control backdrop made
- [ ] Demo script written, timed at 2 minutes
- [ ] Pitch draft, including the "it runs on a laptop" line
- [ ] Packing checklist (power strip, extension cord, USB hub, cables, hotspot)

**Done when:** we could walk into Davis Hall tomorrow and start building with nothing missing.

## Phase 2: At the event (24 hours)

Hours count from when hacking starts.

| Hour | Milestone | Done when | Owner |
| --- | --- | --- | --- |
| H2 | **Hardware alive** | Laptop sends an angle over serial, laser moves there. Tilt limit and watchdog work. | Gio |
| H4 | **Camera sees the dot** | OpenCV finds the red dot in the live feed on the bare table | Gio + AI vision |
| H6 | **Calibration** | One button: laser sweeps, Houston learns pixel → servo angle. Under 1 minute. Dot lands within ~1 cm of a clicked spot. | Gio |
| H8 | **Find (typed)** | Type "keys," YOLO-World finds them, closed-loop correction puts the dot on them | AI vision + Gio |
| H10 | **Find (voice) = MVP** | Say "Houston, where are my keys?", dot lands, Houston answers out loud, about 3 s total | Voice |
| H14 | **Memory + Guide** | Moved item: Houston says where it last was and shows the photo. Multimeter: points at each step. | AI vision |
| H16 | **Mission control screen** | Live view, memory log, accuracy scoreboard updating in real time | Voice + screen |
| H18 | **Gate: Check and Notice** | Only start these if Find passes 18/20. Otherwise spend the time on reliability. | Everyone |
| H20 | **Feature freeze** | No new features after this. Only bug fixes. | Everyone |
| H22 | **Rehearsed** | Full demo run 3 times on a different table, including teardown and recalibration. Devpost and video done. | Everyone |
| H24 | **Judging** | | |

**Save points:** tag a working version in git at H10, H16 and H20. If something breaks late, we roll back instead of debugging during judging.

## What we show judges

A feature only goes in the demo if it hits these in rehearsal:

| Feature | Must pass |
| --- | --- |
| Find | 18/20 |
| Guide | 9/10 full walkthroughs |
| Memory | 9/10 |
| Check / Notice | 9/10, otherwise mention it but don't demo it |
| Recalibration on a new table | 3/3 under 1 minute |

The scoreboard on the mission control screen should show these real numbers.

## Final deliverables

- Working Houston unit that sets up on any table in under a minute
- Mission control screen with live accuracy numbers
- 2-minute demo, rehearsed
- Devpost page with a short demo video
- Pi vs. laptop speed numbers for when judges ask
