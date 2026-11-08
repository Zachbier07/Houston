"""Resizable Tkinter interface with live video and a searchable chat log."""
import glob
import os
import queue
import re
import sys
import tkinter as tk
import time
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

from .camera_app import ZOOM_MAX, ZOOM_MIN, open_camera
from .laser_controller import LaserController
from .vision import CONFIDENCE_THRESHOLD, digital_zoom
from .voice_listener import VoiceListener, extract_command_target


class CameraApp:
    """Camera preview, chat history/input, and native device controls."""

    def __init__(self, cv2_module, camera, camera_index, analyzer, state,
                 voice_listener=None, aimer=None):
        self.cv2 = cv2_module
        self.camera = camera
        self.camera_index = camera_index
        self.analyzer = analyzer
        self.state = state
        self.voice_listener = voice_listener
        self.speaker = None
        self.root = tk.Tk()
        self.root.title("Houston — desk helper")
        self.root.geometry("1280x820")
        self.root.minsize(900, 600)
        self.root.configure(bg="#10151d")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.zoom = tk.DoubleVar(value=ZOOM_MIN)
        self.status = tk.StringVar(value="Starting camera…")
        self.latest_frame = None
        self.photo = None
        self.closed = False
        self.boxes_visible_until = 0.0
        self.camera_indices = self._get_camera_indices()
        self.microphones = self._get_microphones()
        self.ui_events = queue.Queue()
        self.last_logged_request = self.state.request_id
        self.raw_frame = None          # un-zoomed frame: what laser aiming works in
        self.preview_geom = None       # (scale, image_width, image_height) of the shown preview
        # Laser (None when no Uno is connected). Its messages go into the chat.
        self.laser = (LaserController(aimer, lambda text: self.ui_events.put(("Houston", text)),
                                      speak=lambda text: self.speaker and self.speaker.speak(text))
                      if aimer else None)
        self.laser_status = tk.StringVar(value=self.laser.status() if self.laser
                                         else "Laser: no Arduino connected")

        self._build_ui()
        self._select_initial_devices()
        self._bind_keys()

    def _build_ui(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#10151d")
        style.configure("Panel.TFrame", background="#1a222e")
        style.configure("Header.TFrame", background="#151c26")
        style.configure("Video.TLabel", background="#0b1017", foreground="#91a0b2")
        style.configure("TLabel", background="#10151d", foreground="#edf2f8",
                        font=("Sans", 10))
        style.configure("Title.TLabel", font=("Sans", 19, "bold"),
                        foreground="#f4f7fb")
        style.configure("Section.TLabel", background="#1a222e",
                        foreground="#f4f7fb", font=("Sans", 13, "bold"))
        style.configure("Muted.TLabel", foreground="#91a0b2")
        style.configure("Status.TLabel", background="#151c26", foreground="#9adbc9",
                        font=("Sans", 9))
        style.configure("TButton", padding=(12, 8), background="#293545",
                        foreground="#edf2f8", borderwidth=0,
                        font=("Sans", 10, "bold"))
        style.map("TButton", background=[("active", "#35465b"),
                                          ("pressed", "#243141")])
        style.configure("Accent.TButton", padding=(14, 9), background="#16a085",
                        foreground="white", borderwidth=0,
                        font=("Sans", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#1bb596"),
                                                 ("pressed", "#12836e")])
        style.configure("TCombobox", padding=(8, 7), fieldbackground="#222d3a",
                        background="#222d3a", foreground="#edf2f8",
                        arrowcolor="#9adbc9", borderwidth=0)
        style.map("TCombobox", fieldbackground=[("readonly", "#222d3a")],
                  foreground=[("readonly", "#edf2f8")])
        style.configure("TEntry", padding=(10, 9), fieldbackground="#222d3a",
                        foreground="#edf2f8", insertcolor="#edf2f8",
                        borderwidth=0)
        style.configure("Horizontal.TScale", background="#1a222e",
                        troughcolor="#303d4d")
        style.configure("Vertical.TScrollbar", background="#293545",
                        troughcolor="#151c26", borderwidth=0,
                        arrowcolor="#91a0b2")

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 15))
        header.pack(fill="x")
        ttk.Label(header, text="Houston", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="DESK ASSISTANT", style="Status.TLabel").pack(
            side="left", padx=(12, 18))
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(
            side="left", fill="x", expand=True, padx=8)
        ttk.Button(header, text="Quit", command=self.close).pack(side="right")

        device_panel = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 12))
        device_panel.pack(fill="x", padx=14, pady=(12, 10))
        device_bar = ttk.Frame(device_panel, style="Panel.TFrame")
        device_bar.pack(fill="x")
        ttk.Label(device_bar, text="CAMERA", style="Muted.TLabel").pack(
            side="left", padx=(0, 8))
        self.camera_choice = ttk.Combobox(
            device_bar, state="readonly", width=17,
            values=[f"Camera {index}" for index in self.camera_indices])
        self.camera_choice.pack(side="left", padx=(0, 18))
        self.camera_choice.bind("<<ComboboxSelected>>", self._on_camera_selected)

        ttk.Label(device_bar, text="MICROPHONE", style="Muted.TLabel").pack(
            side="left", padx=(0, 8))
        mic_values = [f"{name} ({index if index is not None else 'default'})"
                      for index, name in self.microphones]
        self.mic_choice = ttk.Combobox(device_bar, state="readonly", width=42,
                                       values=mic_values)
        self.mic_choice.pack(side="left", fill="x", expand=True)
        self.mic_choice.bind("<<ComboboxSelected>>", self._on_microphone_selected)

        main = ttk.Panedwindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        preview_panel = ttk.Frame(main, style="Panel.TFrame", padding=12)
        chat_panel = ttk.Frame(main, style="Panel.TFrame", padding=14)
        main.add(preview_panel, weight=3)
        main.add(chat_panel, weight=2)

        self.preview = ttk.Label(preview_panel, anchor="center", style="Video.TLabel",
                                 text="Waiting for camera…")
        self.preview.pack(fill="both", expand=True)
        # Clicks on the video drive the laser: left = aim / calibrate, right = "dot is here".
        self.preview.bind("<Button-1>", lambda event: self._on_preview_click(event, right=False))
        self.preview.bind("<Button-3>", lambda event: self._on_preview_click(event, right=True))
        ttk.Label(preview_panel, textvariable=self.laser_status, style="Muted.TLabel",
                  wraplength=760).pack(fill="x", pady=(6, 0))
        zoom_row = ttk.Frame(preview_panel, style="Panel.TFrame")
        zoom_row.pack(fill="x", pady=(8, 2))
        ttk.Label(zoom_row, text="ZOOM", style="Muted.TLabel").pack(side="left")
        self.zoom_label = ttk.Label(zoom_row, text="1.0×", style="Muted.TLabel")
        self.zoom_label.pack(side="right")
        ttk.Scale(zoom_row, from_=ZOOM_MIN, to=ZOOM_MAX, variable=self.zoom,
                  command=self._on_zoom).pack(side="left", fill="x", expand=True,
                                              padx=12)

        ttk.Label(chat_panel, text="Conversation",
                  style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        ttk.Label(chat_panel, text="Ask a question or type what to find.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        log_frame = ttk.Frame(chat_panel, style="Panel.TFrame")
        log_frame.pack(fill="both", expand=True)
        self.chat_log = tk.Text(log_frame, wrap="word", state="disabled",
                                bg="#151c26", fg="#dfe7f0", insertbackground="white",
                                selectbackground="#276b61", relief="flat", padx=14,
                                pady=12, font=("Sans", 10), spacing3=5,
                                highlightthickness=1, highlightbackground="#293545")
        self.chat_log.tag_configure("you", foreground="#70d9c1", spacing1=10,
                                    font=("Sans", 10, "bold"))
        self.chat_log.tag_configure("houston", foreground="#f0c978", spacing1=10,
                                    font=("Sans", 10, "bold"))
        chat_scroll = ttk.Scrollbar(log_frame, command=self.chat_log.yview)
        self.chat_log.configure(yscrollcommand=chat_scroll.set)
        self.chat_log.pack(side="left", fill="both", expand=True)
        chat_scroll.pack(side="right", fill="y")

        input_row = ttk.Frame(chat_panel, style="Panel.TFrame")
        input_row.pack(fill="x", pady=(10, 0))
        self.entry = ttk.Entry(input_row, font=("Sans", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.entry.bind("<Return>", self._send_chat)
        ttk.Button(input_row, text="Send", style="Accent.TButton",
                   command=self._send_chat).pack(side="right")

        actions = ttk.Frame(self.root, padding=(14, 0, 14, 14))
        actions.pack(fill="x")
        ttk.Button(actions, text="List visible objects", command=self.capture_all).pack(
            side="left")
        ttk.Label(actions, text="Dashed yellow boxes indicate possible matches.",
                  style="Muted.TLabel").pack(side="left", padx=12)

    def _bind_keys(self):
        self.root.bind("<Up>", lambda _event: self._change_zoom(0.1))
        self.root.bind("<Down>", lambda _event: self._change_zoom(-0.1))
        self.root.bind("c", lambda event: self._laser_key(event) or self._cycle_camera())
        self.root.bind("C", lambda event: self._laser_key(event) or self._cycle_camera())
        self.root.bind("<Escape>", lambda event: self._laser_key(event) or self.close())
        self.root.bind("<Key>", self._laser_key)

    def _laser_key(self, event):
        """Laser keys: K calibrate, M move mode, and everything inside those modes.
        Returns True if the laser used the key. Ignored while typing in the chat box."""
        if not self.laser or getattr(event, "widget", None) is self.entry:
            return False
        key = event.char if event.char and event.char.isprintable() and event.char != " " else event.keysym
        return self.laser.handle_key(key)

    def _on_preview_click(self, event, right):
        """Turn a click on the (scaled, centered) preview into raw camera pixels."""
        self.preview.focus_set()      # leave the chat box so laser keys work
        if not self.laser or self.preview_geom is None or self.raw_frame is None:
            return
        scale, image_w, image_h = self.preview_geom
        x = (event.x - (self.preview.winfo_width() - image_w) / 2) / scale
        y = (event.y - (self.preview.winfo_height() - image_h) / 2) / scale
        height, width = self.raw_frame.shape[:2]
        if not (0 <= x < width and 0 <= y < height):
            return                    # clicked the border around the image
        raw_x, raw_y = self._view_to_raw(x / width, y / height)
        self.laser.click(raw_x, raw_y, right=right)

    def _view_to_raw(self, fx, fy, zoom=None, size=None):
        """Fraction of the (zoomed) view -> pixel in the raw camera frame.
        Pass zoom and size (width, height) when calling from a worker thread:
        Tk variables may only be read on the UI thread."""
        width, height = size or (self.raw_frame.shape[1], self.raw_frame.shape[0])
        zoom = self.zoom.get() if zoom is None else zoom
        crop_w, crop_h = width / zoom, height / zoom
        return (width - crop_w) / 2 + fx * crop_w, (height - crop_h) / 2 + fy * crop_h

    def _get_microphones(self):
        if not self.voice_listener:
            return []
        try:
            devices = self.voice_listener.input_devices()
            if devices:
                return devices
        except Exception as exc:
            print(f"Could not list microphones: {exc}", file=sys.stderr, flush=True)
        return [(None, "System default microphone")]

    def attach_voice_listener(self, voice_listener):
        """Attach the listener after circular callbacks have been connected."""
        self.voice_listener = voice_listener
        self.microphones = self._get_microphones()
        self.mic_choice.configure(values=[
            f"{name} ({index if index is not None else 'default'})"
            for index, name in self.microphones
        ])
        self._select_initial_devices()

    def _get_camera_indices(self):
        indices = {self.camera_index}
        if sys.platform.startswith("linux"):
            for path in glob.glob("/dev/video*"):
                suffix = path.rsplit("video", 1)[-1]
                if suffix.isdigit():
                    indices.add(int(suffix))
        if len(indices) == 1:
            indices.update(range(5))
        return sorted(indices)

    def _select_initial_devices(self):
        if self.camera_indices:
            position = (self.camera_indices.index(self.camera_index)
                        if self.camera_index in self.camera_indices else 0)
            self.camera_choice.current(position)
        if not self.microphones:
            self.mic_choice.set("No microphone available")
            return
        preferred = self.voice_listener.device_index
        if preferred is None:
            try:
                import sounddevice as sd
                preferred = sd.default.device[0]
            except Exception:
                pass
        mic_position = next((pos for pos, (device_id, _name)
                             in enumerate(self.microphones) if device_id == preferred), 0)
        self.mic_choice.current(mic_position)

    def run(self):
        if self.voice_listener:
            self.voice_listener.start()
        self._append_chat("Houston", "Ready. Ask a question or tell me what to find.")
        self._update()
        try:
            self.root.mainloop()
        finally:
            self.close()

    def _update(self):
        if self.closed:
            return
        ok, frame = self.camera.read()
        if ok:
            self.raw_frame = frame
            if self.laser:
                self.laser.frame_size = (frame.shape[1], frame.shape[0])
            self.latest_frame = digital_zoom(frame, self.zoom.get(), self.cv2)
            self._render_preview()
        if self.laser:
            self.laser_status.set(self.laser.status())
        self._process_ui_events()
        self._log_completed_request()
        self.status.set(self.state.view()[1])
        self.root.after(15, self._update)

    def _render_preview(self):
        items, _message, busy, snapshot = self.state.view()
        # Keep the camera feed live after analysis; only the box coordinates come
        # from the captured frame, so they stay overlaid on each new preview frame.
        frame = self.latest_frame.copy()
        if snapshot is not None and not busy and time.monotonic() < self.boxes_visible_until:
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
        if self.laser:
            self.laser.draw(frame, self.zoom.get(), self.cv2)
        preview_width = max(320, self.preview.winfo_width())
        preview_height = max(240, self.preview.winfo_height())
        scale = min(preview_width / frame.shape[1], preview_height / frame.shape[0])
        size = (max(1, int(frame.shape[1] * scale)),
                max(1, int(frame.shape[0] * scale)))
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb).resize(size, Image.Resampling.LANCZOS)
        self.preview_geom = (scale, size[0], size[1])
        self.photo = ImageTk.PhotoImage(image=image)
        self.preview.configure(image=self.photo, text="")

    def _draw_dashed_box(self, frame, box, color, dash=10):
        x1, y1, x2, y2 = box
        for left in range(x1, x2, dash * 2):
            right = min(left + dash, x2)
            self.cv2.line(frame, (left, y1), (right, y1), color, 2)
            self.cv2.line(frame, (left, y2), (right, y2), color, 2)
        for top in range(y1, y2, dash * 2):
            bottom = min(top + dash, y2)
            self.cv2.line(frame, (x1, top), (x1, bottom), color, 2)
            self.cv2.line(frame, (x2, top), (x2, bottom), color, 2)

    def _append_chat(self, speaker, text):
        tag = "you" if speaker == "You" else "houston"
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", f"{speaker}: {text}\n\n", tag)
        self.chat_log.configure(state="disabled")
        self.chat_log.see("end")

    def _process_ui_events(self):
        while True:
            try:
                speaker, text = self.ui_events.get_nowait()
            except queue.Empty:
                break
            self._append_chat(speaker, text)

    def _log_completed_request(self):
        request_id = self.state.request_id
        if request_id == self.last_logged_request or self.state.busy:
            return
        items, message, _busy, snapshot = self.state.view()
        self.last_logged_request = request_id
        if snapshot is not None and not _busy:
            self.boxes_visible_until = time.monotonic() + 10.0 if items else 0.0
        if self.state.error:
            answer = f"I couldn't complete that request: {message}"
        elif items:
            lines = []
            for item in items:
                x, y = item["center"]
                certainty = ("Possible" if item.get("confidence", 1.0) < CONFIDENCE_THRESHOLD
                             else "Found")
                line = f"{certainty} {item['label']} at the {self.analyzer._spoken_location((x, y))}"
                nearby = item.get("nearby", [])[:2]
                if nearby:
                    line += f"; beside {', '.join(nearby)}"
                if item.get("note"):
                    line += f" ({item['note']})"
                lines.append(line)
            answer = "\n".join(lines)
            if self.laser and self.raw_frame is not None and self.latest_frame is not None:
                raw_x, raw_y = self._view_to_raw(*items[0]["center"])
                # the box is in pixels of the (zoomed) view; convert its corners to raw
                vh, vw = self.latest_frame.shape[:2]
                x1, y1, x2, y2 = items[0]["box"]
                bx1, by1 = self._view_to_raw(x1 / vw, y1 / vh)
                bx2, by2 = self._view_to_raw(x2 / vw, y2 / vh)
                self.laser.point_at_item(items[0]["label"], raw_x, raw_y, box=(bx1, by1, bx2, by2))
        elif snapshot is not None and message == "Found 0 objects":
            answer = "I couldn't find a clear match in that image."
        else:
            answer = message
            with self.state.lock:
                steps = list(getattr(self.state, "steps", []))
            if steps:
                answer += "\n" + "\n".join(f"{i}. {st['label']}: {st['say']}"
                                            for i, st in enumerate(steps, 1))
                self._start_explanation(message, steps)
        self._append_chat("Houston", answer)

    def _start_explanation(self, message, steps):
        """Speak the answer and point at each step (the laser tour)."""
        spoken = getattr(self.analyzer, "last_spoken_answer", "") or message.split("\n")[0]

        def speak_blocking(text):
            if self.speaker:
                self.speaker._speak(text)          # returns when the sentence has played
            else:
                time.sleep(min(5.0, 0.6 + len(text) / 15))

        if self.laser and self.raw_frame is not None:
            raw_steps = []
            zoom = self.zoom.get()                       # read Tk state here, on the UI thread
            size = (self.raw_frame.shape[1], self.raw_frame.shape[0])
            for st in steps:
                x, y = self._view_to_raw(*st["center"])
                raw_steps.append({"label": st["label"], "say": st["say"], "x": x, "y": y,
                                  # the zoom-in refinement may still be running: the tour
                                  # waits for it, then reads the final spot
                                  "ready": st.get("ready"),
                                  "resolve": lambda st=st: self._view_to_raw(*st["center"], zoom, size)})
            self.laser.explain(spoken, raw_steps, speak_blocking)
        elif self.speaker:
            self.speaker.speak(spoken + " " + " ".join(st["say"] for st in steps))

    def _send_chat(self, _event=None):
        text = self.entry.get().strip()
        if not text:
            return "break"
        self.entry.delete(0, "end")
        self._append_chat("You", text)
        if self.latest_frame is None:
            self._append_chat("Houston", "The camera is not ready yet.")
            return "break"
        request = re.sub(r"^houston[,:]?\s*", "", text, flags=re.IGNORECASE)
        if not request.strip():
            self._append_chat("Houston", "Hi!")
            if self.speaker:
                self.speaker.speak("Hi!")
            return "break"
        if VoiceListener._is_greeting(request):
            self._append_chat("Houston", "Hi!")
            if self.speaker:
                self.speaker.speak("Hi!")
        elif (VoiceListener._is_question(request)
              and not VoiceListener._is_object_search(request)):
            self.analyzer.answer_question(self.latest_frame.copy(), request)
        else:
            target = extract_command_target(request)
            if target:
                self.analyzer.start(self.latest_frame.copy(), target)
            else:
                self._append_chat("Houston", "Tell me what to find or ask me a question.")
        return "break"

    def capture_all(self):
        if self.latest_frame is not None:
            self._append_chat("You", "List the visible objects.")
            self.analyzer.start(self.latest_frame.copy(), "")

    def _on_camera_selected(self, _event=None):
        selected = self.camera_choice.get()
        try:
            index = int(selected.rsplit(" ", 1)[-1])
        except ValueError:
            return
        if index == self.camera_index:
            return
        new_camera, opened_index = open_camera(self.cv2, index)
        if new_camera is None:
            self.status.set(f"Could not open camera {index}")
            self._set_camera_choice(self.camera_index)
            return
        self.camera.release()
        self.camera = new_camera
        self.camera_index = opened_index
        self.state.reset_camera(opened_index)

    def _set_camera_choice(self, index):
        try:
            self.camera_choice.current(self.camera_indices.index(index))
        except ValueError:
            pass

    def _cycle_camera(self):
        if len(self.camera_indices) > 1:
            pos = self.camera_indices.index(self.camera_index)
            self.camera_choice.current((pos + 1) % len(self.camera_indices))
            self._on_camera_selected()

    def _on_microphone_selected(self, _event=None):
        if not self.voice_listener or not self.microphones:
            return
        position = self.mic_choice.current()
        if position >= 0:
            device_index, name = self.microphones[position]
            self.voice_listener.set_device(device_index)
            self.status.set(f"Microphone changed to {name}")

    def _on_zoom(self, value):
        self.zoom_label.configure(text=f"{float(value):.1f}×")

    def _change_zoom(self, amount):
        self.zoom.set(max(ZOOM_MIN, min(ZOOM_MAX, self.zoom.get() + amount)))
        self._on_zoom(self.zoom.get())

    def handle_voice_command(self, target):
        self.ui_events.put(("You", f"Find {target}."))
        if self.latest_frame is not None:
            self.analyzer.start(self.latest_frame.copy(), target)

    def handle_voice_question(self, question):
        self.ui_events.put(("You", question))
        if self.latest_frame is not None:
            self.analyzer.answer_question(self.latest_frame.copy(), question)

    def handle_voice_greeting(self, reply):
        self.ui_events.put(("Houston", reply))
        if self.speaker:
            self.speaker.speak(reply)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.voice_listener:
            self.voice_listener.stop()
        if self.camera:
            self.camera.release()
            self.camera = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def gui_startup_error(_cv2_module=None):
    """Check Tkinter and desktop display availability before app startup."""
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "No graphical display is available; run from a desktop session."
    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as exc:
        return f"Could not initialize the Tkinter desktop window: {exc}"
    return ""
