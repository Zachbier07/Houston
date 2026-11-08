"""Webcam window, drawing, and keyboard controls."""
import glob
import os
import sys
import textwrap
import threading

import numpy as np

from .vision import CONFIDENCE_THRESHOLD, digital_zoom


ZOOM_MIN = 1.0
ZOOM_MAX = 4.0
ZOOM_STEP = 1.1
EXPOSURE_STEP = 5.0
EXPOSURE_MIN = -13.0
EXPOSURE_MAX = 0.0
CONTROL_DOCK_HEIGHT = 150
CONTROL_MIN_WIDTH = 1080

KEY_UP = (82, 65362, 0xFF52, 2490368, 63232)
KEY_DOWN = (84, 65364, 0xFF54, 2621440, 63233)
KEY_LEFT = (81, 65361, 0xFF51, 2424832, 63234)
KEY_RIGHT = (83, 65363, 0xFF53, 2555904, 63235)


def open_camera(cv2, start_index):
    """Open a webcam and release it if it cannot provide a frame."""
    camera = cv2.VideoCapture(start_index)
    # Ask for 16:9. The default 640x480 (4:3) crops the left and right of the view.
    # MJPG first: raw YUYV at 1280x720 is capped at 5 fps over USB 2.0, MJPG does 30.
    camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    camera.set(cv2.CAP_PROP_FPS, 30)
    if camera.isOpened():
        ok, _ = camera.read()
        if ok:
            return camera, start_index
    camera.release()
    return None, start_index


def open_available_camera(cv2, preferred_index, max_indices=10):
    """Try the preferred webcam, then scan the remaining common indices."""
    indices = [preferred_index]
    indices.extend(index for index in range(max_indices) if index != preferred_index)
    for index in indices:
        camera, opened_index = open_camera(cv2, index)
        if camera is not None:
            if opened_index != preferred_index:
                print(f"Camera {preferred_index} unavailable; using camera {opened_index} instead.")
            return camera, opened_index
    return None, preferred_index


def gui_startup_error(cv2):
    """Explain when OpenCV cannot create a desktop window."""
    gui_line = next((line.strip() for line in cv2.getBuildInformation().splitlines()
                     if line.strip().startswith("GUI:")), "GUI: NONE")
    if gui_line.endswith("NONE"):
        return "OpenCV is a headless build. Install the GUI package: opencv-python."
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        if os.environ.get("WAYLAND_DISPLAY"):
            plugin_dirs = [os.path.join(os.path.dirname(cv2.__file__),
                                        "qt", "plugins", "platforms")]
            extra_path = os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH")
            if extra_path:
                plugin_dirs.append(extra_path)
            has_wayland_plugin = any(
                glob.glob(os.path.join(path, "libqwayland*.so")) for path in plugin_dirs
            )
            if not has_wayland_plugin:
                return ("No DISPLAY is available, and this OpenCV Qt build has no Wayland plugin. "
                        "Run from an XWayland/X11 session with DISPLAY set.")
        else:
            return "No graphical display is available; run from an X11 or Wayland desktop session."
    return ""


class CameraApp:
    """Owns webcam capture, drawing, clickable controls, and keyboard controls."""

    WINDOW = "Houston - camera object finder"   # ASCII only: Qt can't find non-ASCII window names

    def __init__(self, cv2, camera, camera_index, analyzer, state, voice_listener=None,
                 aimer=None):
        self.cv2 = cv2
        self.camera = camera
        self.camera_index = camera_index
        self.analyzer = analyzer
        self.state = state
        self.voice_listener = voice_listener
        self.zoom = ZOOM_MIN
        self.prompt_active = False
        self.prompt_text = ""
        self.latest_frame = None
        self.raw_frame = None          # un-zoomed frame, what the laser aiming works in
        self.aimer = aimer             # app.aim.Aimer, or None when no Uno is connected
        self.aiming = False
        self.last_aimed_request = 0
        self.cal = None                # dict while click-calibrating the laser (K)
        self.move = None               # dict while in servo move mode (M)
        self.last_target = None        # (label, x, y) of the last thing the laser aimed at
        self.reach_px = None           # outline of where the laser can reach, raw pixels
        if aimer and aimer.calibrated():
            self.reach_px = aimer.reach_outline()
        self.answer_scroll = 0
        self.quit_requested = False
        self.mouse_callback_installed = False
        self.mouse_callback_failed = False
        self.microphones = self._list_microphones()
        self.microphone_position = self._initial_microphone_position()

    def run(self):
        if self.voice_listener:
            self.voice_listener.start()
        try:
            self.cv2.namedWindow(self.WINDOW, self.cv2.WINDOW_NORMAL)
            self.cv2.resizeWindow(self.WINDOW, 1280, 900)
            while True:
                ok, frame = self.camera.read()
                if not ok:
                    continue
                self.raw_frame = frame
                view_frame = digital_zoom(frame, self.zoom, self.cv2)
                self.latest_frame = view_frame.copy()
                self._aim_at_new_result(frame.shape)
                self._show(view_frame)
                key = self.cv2.waitKeyEx(1)
                self._install_mouse_callback()
                if self.quit_requested or not self._handle_key(key, view_frame):
                    break
        finally:
            if self.voice_listener:
                self.voice_listener.stop()
            self.camera.release()
            try:
                self.cv2.destroyAllWindows()
            except self.cv2.error:
                pass

    def _show(self, live_frame):
        items, message, busy, snapshot = self.state.view()
        view = snapshot.copy() if snapshot is not None and not busy else live_frame.copy()
        if snapshot is not None and not busy:
            self._draw_boxes(view, items)
        if self.prompt_active:
            message = f"Find object: {self.prompt_text}_"
        if self.cal:
            view = live_frame.copy()   # always show the live feed while calibrating
            message = self._cal_status(view)
        if self.move:
            view = live_frame.copy()
            m = self.move
            message = (f"MOVE: A/D mount, W/S laser, [ ] step {m['step']}us, C center, M/ESC done | "
                       f"pan {m['pan']} tilt {m['tilt']} | Uno: {m['reply']}")
        self._draw_reach(view)
        status_message = message
        if snapshot is not None and not busy and not items and len(message) > 90:
            status_message = "Full answer is in the panel on the right"
        self._draw_status(view, status_message)
        canvas = self._draw_panel(view, items, busy, snapshot, message)
        if self.prompt_active:
            self._draw_prompt(canvas, view.shape[1])
        self._draw_controls(canvas, view.shape[1], view.shape[0])
        try:
            self.cv2.imshow(self.WINDOW, canvas)
        except self.cv2.error as exc:
            print("OpenCV could not open its camera window. Use a GUI-enabled "
                  "opencv-python build and launch from a graphical desktop session.",
                  file=sys.stderr)
            print(exc, file=sys.stderr)
            raise

    def _install_mouse_callback(self):
        """Attach click handling after waitKeyEx has created the native window."""
        if self.mouse_callback_installed or self.mouse_callback_failed:
            return
        try:
            self.cv2.setMouseCallback(self.WINDOW, self._on_mouse)
            self.mouse_callback_installed = True
        except self.cv2.error as exc:
            # A HighGUI backend can reject callbacks while its window is starting.
            # Keep the camera app usable via keyboard instead of aborting launch.
            self.mouse_callback_failed = True
            print(f"Mouse controls unavailable; keyboard controls remain active: {exc}",
                  file=sys.stderr, flush=True)

    def _draw_boxes(self, frame, items):
        for item in items:
            x1, y1, x2, y2 = item["box"]
            uncertain = item.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
            color = (0, 220, 255) if uncertain else (0, 0, 255)
            if uncertain:
                self._draw_dashed_box(frame, item["box"], color)
                label = f"Possible {item['label']} ({item['confidence']:.0%})"
            else:
                self.cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                label = item["label"]
            self.cv2.putText(frame, label, (x1, max(20, y1 - 8)),
                             self.cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2,
                             self.cv2.LINE_AA)

    def _draw_dashed_box(self, frame, box, color, dash=10):
        cv2 = self.cv2
        x1, y1, x2, y2 = box
        for left in range(x1, x2, dash * 2):
            right = min(left + dash, x2)
            cv2.line(frame, (left, y1), (right, y1), color, 2)
            cv2.line(frame, (left, y2), (right, y2), color, 2)
        for top in range(y1, y2, dash * 2):
            bottom = min(top + dash, y2)
            cv2.line(frame, (x1, top), (x1, bottom), color, 2)
            cv2.line(frame, (x2, top), (x2, bottom), color, 2)

    def _draw_status(self, frame, message):
        cv2 = self.cv2
        cv2.putText(frame, f"Camera {self.camera_index}  Zoom: {self.zoom:.1f}x",
                    (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (40, 220, 40), 2,
                    cv2.LINE_AA)
        cv2.putText(frame, message, (12, 56), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (40, 220, 40), 2, cv2.LINE_AA)
        cv2.putText(frame, "Space: search  C: camera  M: move servos  K: calibrate  Click: aim  Right-click: dot is here  Q: quit",
                    (12, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (40, 220, 40), 1,
                    cv2.LINE_AA)

    def _draw_panel(self, frame, items, busy, snapshot, message):
        cv2 = self.cv2
        height, width = frame.shape[:2]
        panel_x = width + 14
        right_panel = max(440, CONTROL_MIN_WIDTH - width)
        canvas = cv2.copyMakeBorder(
            frame, 0, CONTROL_DOCK_HEIGHT, 0, right_panel,
            cv2.BORDER_CONSTANT, value=(28, 28, 28))
        cv2.putText(canvas, "Objects and locations", (panel_x, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.62, (240, 240, 240), 2, cv2.LINE_AA)
        self._panel_text(canvas, "Dashed yellow = possible / partly hidden",
                         panel_x, 50, scale=0.38, color=(0, 220, 255))
        if snapshot is None:
            self._panel_text(canvas, "Press Space to capture", panel_x, 64)
        elif busy:
            self._panel_text(canvas, "Analyzing captured image...", panel_x, 64)
        elif not items:
            if message != "Found 0 objects" and message != "Analysis failed":
                self._draw_answer(canvas, message, panel_x, height)
            else:
                self._panel_text(canvas, "No objects listed", panel_x, 64)
        else:
            visible_rows = max(0, (height - 90) // 42)
            for index, item in enumerate(items[:visible_rows]):
                x, y = item["center"]
                line_y = 64 + index * 42
                self._panel_text(canvas, item["label"][:34], panel_x, line_y,
                                 scale=0.52, color=(0, 220, 255)
                                 if item.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
                                 else (0, 200, 255))
                self._panel_text(canvas,
                                 (f"Possible ({item['confidence']:.0%}); "
                                  f"{(item.get('note') or 'partly obscured')[:36]}"
                                  if item.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
                                  else f"center: {x * 100:.1f}% left, {y * 100:.1f}% top"),
                                 panel_x, line_y + 20, scale=0.42)
            if len(items) > visible_rows:
                self._panel_text(canvas, f"+ {len(items) - visible_rows} more in terminal",
                                 panel_x, height - 12, scale=0.42)
        return canvas

    def _draw_controls(self, canvas, image_width, image_height):
        cv2 = self.cv2
        top = image_height
        width = canvas.shape[1]
        cv2.line(canvas, (0, top), (width, top), (70, 80, 90), 1)

        self._button(canvas, (16, top + 18, 132, top + 60), "CAPTURE ALL",
                     (30, 120, 85))
        self._button(canvas, (150, top + 18, 190, top + 60), "<")
        self._button(canvas, (310, top + 18, 350, top + 60), ">")
        self._panel_text(canvas, f"CAMERA {self.camera_index}", 200, top + 44,
                         scale=0.52, color=(240, 240, 240))

        mic_name = self._selected_microphone_name()
        self._button(canvas, (370, top + 18, 410, top + 60), "<")
        self._button(canvas, (820, top + 18, 860, top + 60), ">")
        self._panel_text(canvas, f"MIC: {mic_name[:48]}", 420, top + 44,
                         scale=0.48, color=(240, 240, 240))
        self._button(canvas, (920, top + 18, min(width - 16, 1040), top + 60),
                     "QUIT", (70, 55, 65))

        self._panel_text(canvas, f"ZOOM  {self.zoom:.1f}x", 24, top + 91,
                         scale=0.45, color=(220, 230, 240))
        self._draw_slider(canvas, (100, top + 108, min(450, image_width + 40), top + 120),
                          (self.zoom - ZOOM_MIN) / (ZOOM_MAX - ZOOM_MIN),
                          (80, 190, 220))

        exposure = self.camera.get(self.cv2.CAP_PROP_EXPOSURE)
        exposure = EXPOSURE_MIN if exposure is None else exposure
        exposure_ratio = (exposure - EXPOSURE_MIN) / (EXPOSURE_MAX - EXPOSURE_MIN)
        self._panel_text(canvas, f"EXPOSURE  {exposure:.1f}", 510, top + 91,
                         scale=0.45, color=(220, 230, 240))
        self._draw_slider(canvas, (620, top + 108, min(width - 30, 1000), top + 120),
                          exposure_ratio, (220, 170, 90))

        self._panel_text(canvas, "Capture all | Space: name search | Drag sliders | C camera | Q quit",
                         20, top + 143, scale=0.40, color=(155, 165, 175))

    def _button(self, canvas, bounds, label, color=(55, 70, 82)):
        x1, y1, x2, y2 = bounds
        cv2 = self.cv2
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, -1)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (100, 115, 125), 1)
        text_size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.43, 1)[0]
        x = x1 + max(5, (x2 - x1 - text_size[0]) // 2)
        y = y1 + (y2 - y1 + text_size[1]) // 2
        cv2.putText(canvas, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.43,
                    (245, 245, 245), 1, cv2.LINE_AA)

    def _draw_slider(self, canvas, bounds, ratio, color):
        x1, y1, x2, y2 = bounds
        ratio = max(0.0, min(1.0, ratio))
        cy = (y1 + y2) // 2
        knob_x = round(x1 + ratio * (x2 - x1))
        self.cv2.line(canvas, (x1, cy), (x2, cy), (85, 95, 105), 5)
        self.cv2.line(canvas, (x1, cy), (knob_x, cy), color, 5)
        self.cv2.circle(canvas, (knob_x, cy), 9, (235, 240, 245), -1)
        self.cv2.circle(canvas, (knob_x, cy), 9, color, 2)

    def _list_microphones(self):
        if not self.voice_listener:
            return []
        try:
            return self.voice_listener.input_devices()
        except Exception as exc:
            print(f"Could not list microphones: {exc}", file=sys.stderr, flush=True)
            return []

    def _initial_microphone_position(self):
        if not self.microphones:
            return 0
        selected = self.voice_listener.device_index
        if selected is not None:
            for position, (device_id, _name) in enumerate(self.microphones):
                if device_id == selected:
                    return position
        try:
            import sounddevice as sd
            default_input = sd.default.device[0]
            for position, (device_id, _name) in enumerate(self.microphones):
                if device_id == default_input:
                    return position
        except Exception:
            pass
        return 0

    def _selected_microphone_name(self):
        if not self.microphones:
            return "unavailable"
        return self.microphones[self.microphone_position][1]

    def _on_mouse(self, event, x, y, flags, _userdata):
        cv2 = self.cv2
        # Clicks on the camera image itself drive the laser (aim / calibrate / correct).
        if (self.latest_frame is not None and x < self.latest_frame.shape[1]
                and y < self.latest_frame.shape[0]):
            self._on_laser_mouse(event, x, y)
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            frame_height = self.latest_frame.shape[0] if self.latest_frame is not None else 0
            if y >= frame_height + 18 and y <= frame_height + 60:
                if 16 <= x <= 132:
                    if self.latest_frame is not None:
                        self.analyzer.start(self.latest_frame.copy(), "")
                elif 150 <= x <= 190:
                    self._switch_camera(-1)
                elif 310 <= x <= 350:
                    self._switch_camera(1)
                elif 370 <= x <= 410:
                    self._switch_microphone(-1)
                elif 820 <= x <= 860:
                    self._switch_microphone(1)
                elif 920 <= x <= 1040:
                    self.quit_requested = True
        if self.latest_frame is None:
            return
        height = self.latest_frame.shape[0]
        if y < height + 102 or y > height + 126:
            return
        if event == cv2.EVENT_LBUTTONDOWN or (
                event == cv2.EVENT_MOUSEMOVE and flags & cv2.EVENT_FLAG_LBUTTON):
            if 100 <= x <= min(450, self.latest_frame.shape[1] + 40):
                ratio = (x - 100) / (min(450, self.latest_frame.shape[1] + 40) - 100)
                self.zoom = ZOOM_MIN + max(0, min(1, ratio)) * (ZOOM_MAX - ZOOM_MIN)
            elif 620 <= x <= min(self._canvas_width(), 1000):
                slider_end = min(self._canvas_width() - 30, 1000)
                ratio = (x - 620) / max(1, slider_end - 620)
                exposure = EXPOSURE_MIN + max(0, min(1, ratio)) * (EXPOSURE_MAX - EXPOSURE_MIN)
                self.camera.set(self.cv2.CAP_PROP_EXPOSURE, exposure)

    def _canvas_width(self):
        if self.latest_frame is None:
            return CONTROL_MIN_WIDTH
        image_width = self.latest_frame.shape[1]
        return image_width + max(440, CONTROL_MIN_WIDTH - image_width)

    def _draw_answer(self, canvas, answer, panel_x, height):
        self._panel_text(canvas, "Answer (use [ and ] to scroll)", panel_x, 66,
                         scale=0.46, color=(0, 220, 255))
        lines = textwrap.wrap(answer, width=48, break_long_words=True)
        visible_lines = max(1, (height - 112) // 20)
        max_scroll = max(0, len(lines) - visible_lines)
        self.answer_scroll = min(self.answer_scroll, max_scroll)
        page = lines[self.answer_scroll:self.answer_scroll + visible_lines]
        for index, line in enumerate(page):
            self._panel_text(canvas, line, panel_x, 94 + index * 20, scale=0.43)

    def _panel_text(self, canvas, text, x, y, scale=0.5, color=(220, 220, 220)):
        self.cv2.putText(canvas, text, (x, y), self.cv2.FONT_HERSHEY_SIMPLEX,
                         scale, color, 1, self.cv2.LINE_AA)

    def _draw_prompt(self, canvas, image_width):
        x = image_width + 14
        self._panel_text(canvas, "Type an object name, then Enter", x, 66,
                         scale=0.48, color=(0, 220, 255))
        self._panel_text(canvas, "Blank = list all objects | Esc = cancel", x, 92,
                         scale=0.42)

    def _handle_key(self, key, frame):
        if self.prompt_active:
            return self._handle_prompt_key(key, frame)
        if self.cal:
            return self._handle_cal_key(key)
        if self.move:
            return self._handle_move_key(key)
        if key in (ord("q"), ord("Q"), 27):
            return False
        if key == ord(" "):
            self.prompt_active = True
            self.prompt_text = ""
            self.answer_scroll = 0
        elif key in KEY_UP:
            self.zoom = min(ZOOM_MAX, self.zoom * ZOOM_STEP)
        elif key in KEY_DOWN:
            self.zoom = max(ZOOM_MIN, self.zoom / ZOOM_STEP)
        elif key in KEY_LEFT:
            self._adjust_exposure(-EXPOSURE_STEP)
        elif key in KEY_RIGHT:
            self._adjust_exposure(EXPOSURE_STEP)
        elif key in (ord("c"), ord("C")):
            self._switch_camera()
        elif key in (ord("k"), ord("K")):
            self._start_cal()
        elif key in (ord("m"), ord("M")):
            self._start_move()
        elif key == ord("["):
            self.answer_scroll = max(0, self.answer_scroll - 3)
        elif key == ord("]"):
            self.answer_scroll += 3
        return True

    def _handle_prompt_key(self, key, frame):
        if key == 27:
            self.prompt_active = False
            self.prompt_text = ""
        elif key in (10, 13):
            target = self.prompt_text.strip()
            self.prompt_active = False
            self.prompt_text = ""
            self.analyzer.start(frame, target)
        elif key in (8, 127, 0x01000003):
            self.prompt_text = self.prompt_text[:-1]
        elif 32 <= key <= 126 and len(self.prompt_text) < 64:
            self.prompt_text += chr(key)
        return True

    def _adjust_exposure(self, delta):
        current = self.camera.get(self.cv2.CAP_PROP_EXPOSURE)
        self.camera.set(self.cv2.CAP_PROP_EXPOSURE, current + delta)

    def _switch_camera(self, direction=1):
        next_index = (self.camera_index + direction) % 10
        for _ in range(9):
            new_camera, opened_index = open_camera(self.cv2, next_index)
            if new_camera is not None:
                self.camera.release()
                self.camera = new_camera
                self.camera_index = opened_index
                self.state.reset_camera(opened_index)
                return
            next_index = (next_index + direction) % 10

    def _switch_microphone(self, direction):
        if not self.microphones or not self.voice_listener:
            self.state.set_notice("No microphone inputs are available")
            return
        self.microphone_position = (self.microphone_position + direction) % len(self.microphones)
        device_index, device_name = self.microphones[self.microphone_position]
        self.state.set_notice(f"Microphone: {device_name[:40]}")
        self.voice_listener.set_device(device_index)

    # ---------------- laser aiming ----------------

    def _aim_at_new_result(self, raw_shape):
        """When a find finishes with results, point the laser at the first one."""
        if not self.aimer or self.aiming or self.cal or self.move:
            return
        with self.state.lock:
            request_id, busy, items = self.state.request_id, self.state.busy, list(self.state.items)
        if busy or request_id == self.last_aimed_request or not items:
            return
        self.last_aimed_request = request_id
        if not self.aimer.calibrated():
            self.state.set_notice("Laser not calibrated: press K first")
            return
        x, y = self._view_to_raw(items[0]["center"], raw_shape)
        label = items[0]["label"]
        self._run_aim_job(lambda: self._point(label, x, y))

    def _view_to_raw(self, center, raw_shape):
        """Normalized center in the zoomed view -> pixel in the raw camera frame."""
        height, width = raw_shape[:2]
        crop_w, crop_h = width / self.zoom, height / self.zoom
        left, top = (width - crop_w) / 2, (height - crop_h) / 2
        return left + center[0] * crop_w, top + center[1] * crop_h

    def _run_aim_job(self, job):
        if not self.aimer:
            self.state.set_notice("No Uno connected: laser aiming is off")
            return
        if self.aiming:
            return
        self.aiming = True

        def wrapper():
            try:
                job()
            except Exception as exc:
                self.state.set_notice(f"Laser error: {exc}")
                print(f"Laser error: {exc}", file=sys.stderr, flush=True)
            finally:
                self.aiming = False

        threading.Thread(target=wrapper, daemon=True).start()

    def _point(self, label, x, y):
        self.last_target = (label, x, y)
        result = self.aimer.point_at(x, y)
        if result["out_of_reach"]:
            message = f"{label} is outside the laser's reach (outside the blue outline)"
        elif result["ok"]:
            pan, tilt = result["pulses"]
            message = f"Laser on {label} (pan {pan}, tilt {tilt})"
        else:
            message = f"Laser couldn't aim at {label}: {result.get('reply')}"
        self.state.set_notice(message)
        print(message, flush=True)

    def _learn(self, raw):
        """The dot is really at `raw` for the servo position we last sent. Learn it."""
        if not self.aimer.calibrated() or self.aimer.last_pulses is None:
            self.state.set_notice("Aim somewhere first, then right-click where the dot really is")
            return
        summary = self.aimer.add_sample(*self.aimer.last_pulses, *raw)
        self.reach_px = self.aimer.reach_outline()
        print(f"Learned from correction: {summary}", flush=True)
        if self.last_target:
            label, x, y = self.last_target
            self._point(label, x, y)
            self.state.set_notice(f"Learned ({len(self.aimer.samples)} points). Re-aimed at {label}.")

    def _draw_reach(self, view):
        """Outline the area the laser can reach (curved, from the calibration model)."""
        if self.reach_px is None or self.cal:
            return
        height, width = view.shape[:2]
        crop_w, crop_h = width / self.zoom, height / self.zoom
        left, top = (width - crop_w) / 2, (height - crop_h) / 2
        pts = (self.reach_px - [left, top]) * self.zoom
        if not np.all(np.isfinite(pts)):
            return
        poly = np.clip(pts, -10000, 10000).astype(np.int32).reshape(-1, 1, 2)
        self.cv2.polylines(view, [poly], True, (255, 160, 0), 2, self.cv2.LINE_AA)

    def _on_laser_mouse(self, event, x, y):
        """Left click: record a calibration point, or aim there.
        Right click: "the dot actually landed HERE" -> learn from it and re-aim."""
        if event not in (self.cv2.EVENT_LBUTTONDOWN, self.cv2.EVENT_RBUTTONDOWN) or self.raw_frame is None:
            return
        height, width = self.raw_frame.shape[:2]
        if x >= width or y >= height:      # click landed on the side panel
            return
        raw = self._view_to_raw((x / width, y / height), self.raw_frame.shape)
        print(f"{'Right' if event == self.cv2.EVENT_RBUTTONDOWN else 'Left'} click at "
              f"({raw[0]:.0f}, {raw[1]:.0f})", flush=True)
        if event == self.cv2.EVENT_RBUTTONDOWN:
            if not self.cal and not self.move:
                self._run_aim_job(lambda: self._learn(raw))
            return
        if self.cal:
            self._cal_click(raw)
        elif self.aimer and self.aimer.calibrated():
            self._run_aim_job(lambda: self._point(f"({raw[0]:.0f}, {raw[1]:.0f})", *raw))

    # ---------------- move mode (M): drive the servos by hand ----------------

    def _start_move(self):
        if not self.aimer:
            self.state.set_notice("No Uno connected: can't move the servos")
            return
        if self.aiming:
            return
        from .aim import CENTER_US
        self.move = {"pan": CENTER_US[0], "tilt": CENTER_US[1], "step": 30, "reply": ""}
        self._send_move()

    def _send_move(self):
        from .aim import PAN_US, TILT_US
        m = self.move
        # Stay inside the firmware limits so the numbers on screen match the servos.
        m["pan"] = min(max(m["pan"], PAN_US[0]), PAN_US[1])
        m["tilt"] = min(max(m["tilt"], TILT_US[0]), TILT_US[1])
        m["reply"] = self.aimer.link.aim_to(m["pan"], m["tilt"])
        print(f"Move: A {m['pan']} {m['tilt']} -> {m['reply']}", flush=True)

    def _handle_move_key(self, key):
        from .aim import CENTER_US
        m = self.move
        moves = {ord("a"): (-1, 0), ord("d"): (1, 0), ord("w"): (0, 1), ord("s"): (0, -1)}
        k = key & 0xFF if 0 <= key < 0x110000 else key
        if k in (ord("A"), ord("D"), ord("W"), ord("S")):
            k = k + 32   # same as lowercase
        if k in moves:
            dp, dt = moves[k]
            m["pan"] += dp * m["step"]
            m["tilt"] += dt * m["step"]
            self._send_move()
        elif k in (ord("c"), ord("C")):
            m["pan"], m["tilt"] = CENTER_US
            self._send_move()
        elif key == ord("["):
            m["step"] = max(5, m["step"] // 2)
        elif key == ord("]"):
            m["step"] = min(200, m["step"] * 2)
        elif key in (27, ord("m"), ord("M")):
            self.move = None
            self.state.set_notice(f"Move mode off at pan {m['pan']} tilt {m['tilt']}")
        return True

    # ---------------- calibration (K): click the dot at each grid point ----------------

    def _start_cal(self):
        if not self.aimer:
            self.state.set_notice("No Uno connected: laser aiming is off")
            return
        if self.aiming:
            return
        self.zoom = ZOOM_MIN               # clicks must be in the raw camera view
        self.cal = {"grid": self.aimer.calibration_grid(), "i": 0, "samples": {}}
        self._cal_move()

    def _cal_move(self):
        pan, tilt = self.cal["grid"][self.cal["i"]]
        self.cal["reply"] = self.aimer.move_to(pan, tilt)   # same-side approach, like aiming

    def _cal_status(self, view):
        cal = self.cal
        for (px, py) in cal["samples"].values():   # clicked points so far
            self.cv2.circle(view, (int(px), int(py)), 6, (0, 220, 0), 2)
        reply = "" if cal.get("reply") == "OK" else f" [{cal.get('reply')}]"
        return (f"CALIBRATE {cal['i'] + 1}/{len(cal['grid'])}: click the laser dot | S: can't see it | "
                f"BKSP: back | ENTER: done ({len(cal['samples'])} clicks) | ESC: cancel{reply}")

    def _cal_click(self, raw):
        self.cal["samples"][self.cal["i"]] = raw
        self._cal_next()

    def _cal_next(self):
        self.cal["i"] += 1
        if self.cal["i"] >= len(self.cal["grid"]):
            self._cal_finish()
        else:
            self._cal_move()

    def _handle_cal_key(self, key):
        cal = self.cal
        if key in (ord("s"), ord("S")):
            cal["samples"].pop(cal["i"], None)
            self._cal_next()
        elif key in (8, 127, 0x01000003) and cal["i"] > 0:
            cal["i"] -= 1
            cal["samples"].pop(cal["i"], None)
            self._cal_move()
        elif key in (10, 13):
            self._cal_finish()
        elif key == 27:
            self.cal = None
            self.state.set_notice("Calibration cancelled")
        return True

    def _cal_finish(self):
        cal, self.cal = self.cal, None
        samples = [(*cal["grid"][i], px, py) for i, (px, py) in sorted(cal["samples"].items())]
        height, width = self.raw_frame.shape[:2]
        try:
            summary = self.aimer.fit(samples, (width, height))
            self.reach_px = self.aimer.reach_outline()
            summary += " Click anywhere to test."
        except Exception as exc:
            summary = f"Calibration failed: {exc}"
        self.state.set_notice(summary)
        print(summary, flush=True)

    def handle_voice_command(self, target):
        """Search the most recent camera frame when the wake phrase is heard."""
        frame = self.latest_frame
        if frame is None:
            self.state.set_notice("Camera frame not ready; try the voice command again")
            return
        self.analyzer.start(frame.copy(), target)

    def handle_voice_question(self, question):
        """Answer a spoken question using the latest camera frame."""
        frame = self.latest_frame
        if frame is None:
            self.state.set_notice("Camera frame not ready; try the question again")
            return
        self.answer_scroll = 0
        self.analyzer.answer_question(frame.copy(), question)
