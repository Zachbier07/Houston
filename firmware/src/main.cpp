// RUNS ON: Arduino Uno.
//
// Houston firmware: a dumb, safe muscle. It moves two servos and switches the
// laser. All the thinking happens on the laptop.
//
// Serial, 115200 baud, one command per line (newline-terminated):
//   A <pan_us> <tilt_us>   aim (servo pulse widths in microseconds)
//   L 1 / L 0              laser on / off
//   H                      heartbeat (laptop sends every 100 ms)
// Replies, one line per command:
//   OK                     done
//   E <reason>             error (see below); for tilt_clamped the move still happened
// Unsolicited lines:
//   READY                  sent once at boot
//   E watchdog             laser forced off: no command for WATCHDOG_MS
//
// Safety rules live HERE, not in Python:
//   1. Laser is off at boot (and the 10k pulldown on the gate keeps it off during reset).
//   2. Servos start centered.
//   3. Tilt is clamped to TILT_MIN_US..TILT_MAX_US so the laser can't aim above the desk.
//   4. No command for 300 ms -> laser off. It stays off until the laptop sends "L 1" again.

#include <Arduino.h>
#include <Servo.h>

// ---- Pins (matches the current test wiring, not docs/houston-parts.md yet) ----
const int PAN_PIN = 8;    // mount / base servo
const int TILT_PIN = 7;   // laser servo
const int LASER_PIN = 4;  // !!! CONFIRM: laser switch (2N7000 gate via 100 ohm), HIGH = on

// ---- Limits ----
// The test sketch's safe range was 45..135 degrees with Servo.write(). With the
// Servo library's default 544..2400 us scale that is 1008..1936 us; 90 deg = 1472 us.
const int PAN_MIN_US = 1008;
const int PAN_MAX_US = 1936;
const int CENTER_US = 1472;

// !!! MEASURE THESE ON THE REAL MOUNT BEFORE TURNING THE LASER ON !!!
// Sweep tilt with the laser OFF and find the pulse range where the laser points
// at the desk. Everything outside this range is clamped. Start narrow.
const int TILT_MIN_US = 1200;
const int TILT_MAX_US = 1800;

const unsigned long WATCHDOG_MS = 300;
const int LINE_MAX = 32;

Servo pan;
Servo tilt;

char line[LINE_MAX];
int lineLen = 0;
bool lineTooLong = false;

bool laserOn = false;
unsigned long lastCommandMs = 0;

void setLaser(bool on) {
  laserOn = on;
  digitalWrite(LASER_PIN, on ? HIGH : LOW);
}

// Returns true if the value had to be clamped.
bool clampInt(int &value, int lo, int hi) {
  if (value < lo) { value = lo; return true; }
  if (value > hi) { value = hi; return true; }
  return false;
}

void handleAim(const char *args) {
  int panUs, tiltUs;
  if (sscanf(args, "%d %d", &panUs, &tiltUs) != 2) {
    Serial.println(F("E bad_args"));
    return;
  }
  clampInt(panUs, PAN_MIN_US, PAN_MAX_US);
  bool tiltClamped = clampInt(tiltUs, TILT_MIN_US, TILT_MAX_US);
  pan.writeMicroseconds(panUs);
  tilt.writeMicroseconds(tiltUs);
  if (tiltClamped) {
    Serial.print(F("E tilt_clamped "));
    Serial.println(tiltUs);
  } else {
    Serial.println(F("OK"));
  }
}

void handleLaser(const char *args) {
  if (strcmp(args, "1") == 0) {
    setLaser(true);
    Serial.println(F("OK"));
  } else if (strcmp(args, "0") == 0) {
    setLaser(false);
    Serial.println(F("OK"));
  } else {
    Serial.println(F("E bad_args"));
  }
}

void handleLine(char *cmd) {
  lastCommandMs = millis();  // any command counts as a heartbeat

  char *args = cmd + 1;
  while (*args == ' ') args++;

  switch (cmd[0]) {
    case 'A': handleAim(args); break;
    case 'L': handleLaser(args); break;
    case 'H': Serial.println(F("OK")); break;
    default:  Serial.println(F("E unknown_command")); break;
  }
}

void readSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLen] = '\0';
      if (lineTooLong) {
        Serial.println(F("E too_long"));
      } else if (lineLen > 0) {
        handleLine(line);
      }
      lineLen = 0;
      lineTooLong = false;
    } else if (lineLen < LINE_MAX - 1) {
      line[lineLen++] = c;
    } else {
      lineTooLong = true;
    }
  }
}

void setup() {
  // Laser off before anything else.
  pinMode(LASER_PIN, OUTPUT);
  setLaser(false);

  // Write the center position before attach so the servos don't jump on boot.
  pan.writeMicroseconds(CENTER_US);
  tilt.writeMicroseconds(CENTER_US);
  pan.attach(PAN_PIN);
  tilt.attach(TILT_PIN);

  Serial.begin(115200);
  lastCommandMs = millis();
  Serial.println(F("READY"));
}

void loop() {
  readSerial();

  if (laserOn && millis() - lastCommandMs > WATCHDOG_MS) {
    setLaser(false);
    Serial.println(F("E watchdog"));
  }
}
