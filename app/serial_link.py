"""RUNS ON: laptop. Talks to the Arduino Uno over USB serial.

Everyone else uses only:
    link = SerialLink()          # finds the Uno, waits for it to boot
    link.aim_to(pan_us, tilt_us)
    link.laser(True / False)
    link.close()

What it does for you:
- Sends a heartbeat ("H") every 100 ms in the background. If the laptop stops
  talking for 300 ms, the firmware turns the laser off on its own.
- Waits for the Uno to finish resetting after the port opens (it prints READY).
- Reconnects if the USB cable gets bumped. After a reconnect the Uno has reset,
  so the laser is OFF and the servos are centered. Nothing turns it back on
  automatically.

Every command returns the Uno's reply: "OK", or "E <reason>"
(for example "E tilt_clamped 1800"), or "E no_link" if the Uno isn't connected.

Try it by hand (H2 milestone check):
    python -m app.serial_link              # auto-detect the port
    python -m app.serial_link /dev/ttyACM0
then type "A 1500 1500", "L 1", "L 0".
"""
import logging
import threading
import time

import serial
from serial.tools import list_ports

log = logging.getLogger("serial_link")

BAUD = 115200
HEARTBEAT_S = 0.1
REPLY_TIMEOUT_S = 0.3
BOOT_TIMEOUT_S = 3.0      # the Uno resets when the port opens; READY arrives ~2 s later
RECONNECT_EVERY_S = 1.0


def find_uno_port():
    """Return the first port that looks like an Arduino, or None."""
    for port in list_ports.comports():
        text = f"{port.description} {port.manufacturer} {port.device}".lower()
        if "arduino" in text or "ttyacm" in text or "ch340" in text or "usb serial" in text:
            return port.device
    return None


class SerialLink:
    def __init__(self, port=None):
        self.port = port
        self.ser = None
        self.lock = threading.Lock()   # one command on the wire at a time
        self.running = True
        self._connect()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    # ---------- public interface ----------

    def aim_to(self, pan_us, tilt_us):
        """Move the servos. Returns "OK" or "E <reason>"."""
        return self._command(f"A {int(pan_us)} {int(tilt_us)}")

    def laser(self, on):
        """Turn the laser on or off. Returns "OK" or "E <reason>"."""
        return self._command("L 1" if on else "L 0")

    def connected(self):
        return self.ser is not None

    def close(self):
        """Laser off, stop the heartbeat, release the port."""
        if self.ser is not None:
            self.laser(False)
        self.running = False
        with self.lock:
            self._drop_port()

    # ---------- internals ----------

    def _connect(self):
        """Open the port and wait for the Uno to boot. Returns True on success."""
        port = self.port or find_uno_port()
        if port is None:
            log.warning("No Arduino found. Is the Uno plugged in?")
            return False
        try:
            ser = serial.Serial(port, BAUD, timeout=REPLY_TIMEOUT_S)
        except serial.SerialException as e:
            log.warning("Can't open %s: %s", port, e)
            return False

        # Opening the port resets the Uno. Wait for READY instead of a fixed sleep.
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline:
            if ser.readline().decode(errors="replace").strip() == "READY":
                break
        else:
            log.warning("No READY from %s after %.0f s. Continuing anyway.", port, BOOT_TIMEOUT_S)

        ser.reset_input_buffer()
        self.ser = ser
        log.info("Connected to Uno on %s", port)
        return True

    def _drop_port(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except serial.SerialException:
                pass
        self.ser = None

    def _command(self, text):
        """Send one line and return the Uno's reply line."""
        with self.lock:
            if self.ser is None:
                return "E no_link"
            try:
                self.ser.write((text + "\n").encode())
                return self._read_reply()
            except (serial.SerialException, OSError) as e:
                log.warning("Lost the Uno (%s). Will reconnect.", e)
                self._drop_port()
                return "E no_link"

    def _read_reply(self):
        """Read until the reply to our command, skipping unsolicited lines."""
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while time.monotonic() < deadline:
            reply = self.ser.readline().decode(errors="replace").strip()
            if not reply:
                continue
            if reply == "E watchdog":
                log.warning("Firmware watchdog turned the laser off (heartbeat gap).")
                continue
            if reply == "READY":
                log.warning("Uno rebooted. Laser is off and servos are centered.")
                continue
            return reply
        return "E timeout"

    def _heartbeat_loop(self):
        while self.running:
            if self.ser is None:
                if not self._connect():
                    time.sleep(RECONNECT_EVERY_S)
                continue
            reply = self._command("H")
            if reply not in ("OK", "E no_link"):
                log.warning("Heartbeat got %r", reply)
            time.sleep(HEARTBEAT_S)


def main():
    """Type commands by hand: A <pan_us> <tilt_us>, L 1, L 0. Ctrl+D or 'q' quits."""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    link = SerialLink(sys.argv[1] if len(sys.argv) > 1 else None)
    print("Commands: A <pan_us> <tilt_us> | L 1 | L 0 | q")
    try:
        while True:
            text = input("> ").strip()
            if text in ("q", "quit"):
                break
            parts = text.split()
            if len(parts) == 3 and parts[0].upper() == "A":
                print(link.aim_to(parts[1], parts[2]))
            elif len(parts) == 2 and parts[0].upper() == "L":
                print(link.laser(parts[1] == "1"))
            elif text:
                print("?  A <pan_us> <tilt_us> | L 1 | L 0 | q")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        link.close()


if __name__ == "__main__":
    main()
