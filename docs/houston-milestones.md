# Houston: Milestones

Nov 7, 2026 · @Gio · Rev 2

Hours count from when hacking started (H0). Every milestone leaves us with something we could demo if time ran out. Who does what in each block is in `houston-build-plan.md`.

## Owners

| Area | Owner |
| --- | --- |
| Laser + aiming | Gio |
| AI vision (camera, brain, memory) | TBD |
| Voice + executor + server | TBD |
| Build + setup + screen | TBD |

## Milestones

| Hour | Milestone | Done when | Owner |
| --- | --- | --- | --- |
| H2 | **Hardware alive** | Laptop sends an angle over serial, laser moves there. Tilt limit and watchdog work. | Gio |
| H4 | **Brain picked** | Bake-off done, model locked, pick written in the stack doc | AI vision |
| H4 | **Camera sees the dot** | The on/off diff finds the dot live on the actual table | Gio + AI vision |
| H6 | **Calibration** | One button: laser sweeps, Houston learns pixel → servo pulse. Under 1 minute. Dot lands within ~1 cm of a clicked spot. | Gio |
| H8 | **Find (typed)** | Type "where are my keys", the brain returns a point, closed-loop correction puts the dot on them | AI vision + Gio |
| H10 | **Find (voice) = MVP** | Say "Hey Houston, where are my keys?", dot lands, Houston answers out loud, 3-4 s total | Voice |
| H14 | **Memory + Guide** | Hidden item: Houston says where it last was and shows the snapshot. Multimeter: points at each step on "next." | AI vision + Voice |
| H16 | **Mission control screen** | Live view, snapshots, accuracy scoreboard updating in real time | Build |
| H18 | **Gate: Check and Notice** | Only start these if Find passes 18/20. Otherwise spend the time on reliability. | Everyone |
| H20 | **Feature freeze** | No new features after this. Only bug fixes. | Everyone |
| H22 | **Rehearsed** | Full demo run 3 times on a different table, including teardown and recalibration. Devpost and video done. | Everyone |
| H24 | **Judging** | | |

**Save points:** tag a working version in git at H10, H16 and H20. If something breaks late, roll back instead of debugging during judging.

## What we show judges

A feature only goes in the demo if it hits these in rehearsal:

| Feature | Must pass |
| --- | --- |
| Find | 18/20 |
| Guide | 9/10 full walkthroughs |
| Memory | 9/10 |
| Check / Notice | 9/10, otherwise mention it but don't demo it |
| Recalibration on a new table | 3/3 under 1 minute |

The scoreboard on the mission control screen shows these real numbers.

## Final deliverables

- Working Houston unit that sets up on any table in under a minute
- Mission control screen with live accuracy numbers
- 2-minute demo, rehearsed
- Devpost page with a short demo video
- Latency numbers per step, for when judges ask
