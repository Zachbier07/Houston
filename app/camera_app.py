"""Webcam window, drawing, and keyboard controls."""
import glob
import os
import sys
import textwrap

from .vision import CONFIDENCE_THRESHOLD, digital_zoom


ZOOM_MIN = 1.0
ZOOM_MAX = 4.0
ZOOM_STEP = 1.1
EXPOSURE_STEP = 5.0
EXPOSURE_MIN = -13.0
EXPOSURE_MAX = 0.0
CONTROL_DOCK_HEIGHT = 150
CONTROL_MIN_WIDTH = 1080


def open_camera(cv2, start_index):
    """Open a webcam and release it if it cannot provide a frame."""
    camera = cv2.VideoCapture(start_index)
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

    WINDOW = "Houston — camera object finder"

    def __init__(self, cv2, camera, camera_index, analyzer, state, voice_listener=None):
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
                view_frame = digital_zoom(frame, self.zoom, self.cv2)
                self.latest_frame = view_frame.copy()
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
        cv2.putText(frame, "Space: name search  Up/Down: zoom  Left/Right: exposure  C: camera  Q: quit",
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
        if key in (ord("q"), ord("Q"), 27):
            return False
        if key == ord(" "):
            self.prompt_active = True
            self.prompt_text = ""
            self.answer_scroll = 0
        elif key in (82, 65362, 0xFF52, 2490368, 63232):
            self.zoom = min(ZOOM_MAX, self.zoom * ZOOM_STEP)
        elif key in (84, 65364, 0xFF54, 2621440, 63233):
            self.zoom = max(ZOOM_MIN, self.zoom / ZOOM_STEP)
        elif key in (81, 65361, 0xFF51, 2424832, 63234):
            self._adjust_exposure(-EXPOSURE_STEP)
        elif key in (83, 65363, 0xFF53, 2555904, 63235):
            self._adjust_exposure(EXPOSURE_STEP)
        elif key in (ord("c"), ord("C")):
            self._switch_camera()
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
