"""Webcam window, drawing, and keyboard controls."""
import glob
import os
import sys

from .vision import digital_zoom


ZOOM_MIN = 1.0
ZOOM_MAX = 4.0
ZOOM_STEP = 1.1
EXPOSURE_STEP = 5.0


def open_camera(cv2, start_index):
    """Open a webcam and release it if it cannot provide a frame."""
    camera = cv2.VideoCapture(start_index)
    if camera.isOpened():
        ok, _ = camera.read()
        if ok:
            return camera, start_index
    camera.release()
    return None, start_index


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
    """Owns webcam capture, window drawing, and keyboard controls."""

    WINDOW = "Houston — camera object finder"

    def __init__(self, cv2, camera, camera_index, analyzer, state):
        self.cv2 = cv2
        self.camera = camera
        self.camera_index = camera_index
        self.analyzer = analyzer
        self.state = state
        self.zoom = ZOOM_MIN
        self.prompt_active = False
        self.prompt_text = ""

    def run(self):
        try:
            while True:
                ok, frame = self.camera.read()
                if not ok:
                    continue
                view_frame = digital_zoom(frame, self.zoom, self.cv2)
                self._show(view_frame)
                key = self.cv2.waitKeyEx(1)
                if not self._handle_key(key, view_frame):
                    break
        finally:
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
        self._draw_status(view, message)
        canvas = self._draw_panel(view, items, busy, snapshot)
        if self.prompt_active:
            self._draw_prompt(canvas, view.shape[1])
        try:
            self.cv2.imshow(self.WINDOW, canvas)
        except self.cv2.error as exc:
            print("OpenCV could not open its camera window. Use a GUI-enabled "
                  "opencv-python build and launch from a graphical desktop session.",
                  file=sys.stderr)
            print(exc, file=sys.stderr)
            raise

    def _draw_boxes(self, frame, items):
        for item in items:
            x1, y1, x2, y2 = item["box"]
            self.cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            self.cv2.putText(frame, item["label"], (x1, max(20, y1 - 8)),
                             self.cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 255), 2,
                             self.cv2.LINE_AA)

    def _draw_status(self, frame, message):
        cv2 = self.cv2
        cv2.putText(frame, f"Camera {self.camera_index}  Zoom: {self.zoom:.1f}x",
                    (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (40, 220, 40), 2,
                    cv2.LINE_AA)
        cv2.putText(frame, message, (12, 56), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (40, 220, 40), 2, cv2.LINE_AA)
        cv2.putText(frame, "Space: capture/list  Up/Down: zoom  Left/Right: exposure  C: camera  Q: quit",
                    (12, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (40, 220, 40), 1,
                    cv2.LINE_AA)

    def _draw_panel(self, frame, items, busy, snapshot):
        cv2 = self.cv2
        height, width = frame.shape[:2]
        panel_x = width + 14
        canvas = cv2.copyMakeBorder(frame, 0, 0, 0, 440, cv2.BORDER_CONSTANT,
                                    value=(28, 28, 28))
        cv2.putText(canvas, "Objects and locations", (panel_x, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.62, (240, 240, 240), 2, cv2.LINE_AA)
        if snapshot is None:
            self._panel_text(canvas, "Press Space to capture", panel_x, 64)
        elif busy:
            self._panel_text(canvas, "Analyzing captured image...", panel_x, 64)
        elif not items:
            self._panel_text(canvas, "No objects listed", panel_x, 64)
        else:
            visible_rows = max(0, (height - 90) // 42)
            for index, item in enumerate(items[:visible_rows]):
                x, y = item["center"]
                line_y = 64 + index * 42
                self._panel_text(canvas, item["label"][:34], panel_x, line_y,
                                 scale=0.52, color=(0, 200, 255))
                self._panel_text(canvas,
                                 f"center: {x * 100:.1f}% left, {y * 100:.1f}% top",
                                 panel_x, line_y + 20, scale=0.42)
            if len(items) > visible_rows:
                self._panel_text(canvas, f"+ {len(items) - visible_rows} more in terminal",
                                 panel_x, height - 12, scale=0.42)
        return canvas

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

    def _switch_camera(self):
        next_index = (self.camera_index + 1) % 10
        for _ in range(9):
            new_camera, opened_index = open_camera(self.cv2, next_index)
            if new_camera is not None:
                self.camera.release()
                self.camera = new_camera
                self.camera_index = opened_index
                self.state.reset_camera(opened_index)
                return
            next_index = (next_index + 1) % 10
