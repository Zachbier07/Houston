# Houston: Final Parts List

Nov 2, 2026 · @Gio

## Have / bought

| Item | Notes |
| --- | --- |
| Arduino Uno | Main board |
| USB-B cables | For the Uno |
| Webcam | Also our mic. Confirm OpenCV can lock exposure and focus. |
| Desk lamp | Overhead mount + lighting |
| Lamp base weight | If the lamp tips or bounces |
| Small USB hub | Rides on the lamp head |
| MOSFETs (2N7000) | Laser switch. The 2-channel relay module is the backup. |
| Capacitors | Across servo power |
| Jumpers, headers, perfboard | |
| 3 × 9g servos | Bench testing and backup for the MG90S |
| MG90S servos | Pan + tilt |
| 1 mW 650 nm laser module | 3 V module, 7 mm × 21 mm. Not 5 mW. |
| 5 V 3 A power adapter | Green screw-terminal tip. Servo power. |
| USB speaker | Plays audio over USB |
| Resistors | 100 Ω (gate) + 10 kΩ (pulldown) |
| Active USB extension, 10 ft | Lamp head to laptop |

Pan-tilt mount: 3D printed (SG90 Tinkercad design), with a 7 mm laser clip added.

## How it's wired

### Pin map (Uno)

| Uno pin | Goes to |
| --- | --- |
| D9 | Pan servo signal (orange) |
| D10 | Tilt servo signal (orange) |
| D7 | MOSFET gate, through a 100 Ω resistor |
| 3.3V | Laser + (red) |
| GND | Common ground: adapter −, both servo browns, MOSFET source |
| USB-B | Laptop (through the hub). Powers the Uno. |
| 5V | **Nothing.** Leave it empty. |

### Servo power rail

```
 5 V 3 A adapter (+) ──┬── pan servo red
                       ├── tilt servo red
                       └── capacitor (+)
 5 V 3 A adapter (−) ──┬── pan servo brown
                       ├── tilt servo brown
                       ├── capacitor (−, striped side)
                       └── Uno GND
```

### Laser switch (low-side, 2N7000)

```
 Uno 3.3V ─────────────► laser + (red)
                          laser − (black/blue) ──► MOSFET drain
 Uno D7 ──[100 Ω]──┬───► MOSFET gate
                   └──[10 kΩ]── GND      (keeps the laser off while the Uno boots)
                          MOSFET source ──► GND
```

2N7000 legs, flat face toward you, legs down: **Source, Gate, Drain** (left to right). Check your part's datasheet, since some clones differ.

Backup: if the MOSFET gives you trouble, the 2-channel relay module does the same job. 3.3V → COM, NO → laser +, laser − → GND. Use NO, not NC. Most of these boards switch on when IN is LOW.

### What rides on the lamp

```
 On the lamp:   camera ──┐
                Uno ─────┼── USB hub ──► USB extension ──► laptop
                servos, laser, MOSFET, cap
                5 V adapter cable runs down the lamp to the wall

 On the table:  speaker ──► laptop USB
 (mic = the webcam's built-in mic, up on the lamp)
```

### Rules

- All grounds tie together: adapter, servos, Uno, MOSFET. Without a shared ground the servos jitter or ignore commands.
- The adapter's +5 V never touches the Uno's 5V pin. The Uno runs off USB.
- Servos never run off the Uno's 5 V. Two MG90S can pull over 1 A when they stall, and that browns out the Uno.
- The laser runs on 3.3 V. It's a 3 V module and 5 V can burn it out. A 1 mW module draws about 20-30 mA, which is fine for the Uno's 3.3V pin (about 50 mA max).
- Capacitor polarity matters. The stripe is the negative side.
- Solder or crimp the power rail. Jumper wires falling out of a lamp head mid-demo is the most likely failure here.
- The Uno resets every time the laptop opens the serial port. The laptop waits about 2 s before sending commands. The 10 kΩ pulldown keeps the laser off during that reset.
- Watch the weight on the lamp head. If it droops, move the Uno and hub to the lamp base and run longer servo leads up.
- Lock or tape the lamp joints so the head can't drift between calibrations.

### Before first power-on

- [ ] Adapter unplugged. Meter in continuity mode: adapter − to Uno GND beeps. Adapter + to Uno 5V does **not** beep.
- [ ] Capacitor stripe on the ground side
- [ ] MOSFET legs match the datasheet
- [ ] Plug in USB first (laser should stay off), then the adapter (servos may twitch once, which is normal)

## Hardware checks

- [ ] OpenCV can turn off auto-exposure and autofocus on the webcam
- [ ] Snap the servos back and forth and watch how long the dot takes to settle. If it bounces, lock the lamp joints and weigh down the base.
- [ ] Lamp on, laser on: the dot still stands out. LED lamps can flicker on camera (banding). If you see it, try another brightness level.
- [ ] The camera sees the whole work area from the lamp head
- [ ] No desk mat: the laser dot shows up on the bare table under bright room light. Test on at least 3 tables (light, dark, glossy if there is one).
- [ ] Webcam mic hears the wake word from about 1 m with noise playing
