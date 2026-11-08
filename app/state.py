"""Thread-safe analysis state shared by UI and worker threads."""
from dataclasses import dataclass, field
import threading


@dataclass
class AnalysisState:
    items: list = field(default_factory=list)
    message: str = "Press Space to capture and analyze"
    error: str = ""
    busy: bool = False
    snapshot: object = None
    request_id: int = 0
    steps: list = field(default_factory=list)   # pointing steps of the last answer
    lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self, frame):
        with self.lock:
            if self.busy:
                return None
            self.request_id += 1
            self.items = []
            self.steps = []
            self.message = "Analyzing snapshot..."
            self.error = ""
            self.busy = True
            self.snapshot = frame.copy()
            return self.request_id

    def set_message(self, request_id, message):
        with self.lock:
            if self.request_id == request_id:
                self.message = message

    def complete(self, request_id, items):
        with self.lock:
            if self.request_id == request_id:
                self.items = items
                self.message = f"Found {len(items)} objects"
                self.error = ""
                self.busy = False

    def complete_answer(self, request_id, answer, steps=None):
        """steps: list of {"label", "say", "center": (x, y) as 0..1 fractions of the frame}."""
        with self.lock:
            if self.request_id == request_id:
                self.items = []
                self.message = answer
                self.steps = list(steps or [])
                self.error = ""
                self.busy = False

    def fail(self, request_id, error):
        with self.lock:
            if self.request_id == request_id:
                self.message = "Analysis failed"
                self.error = error
                self.busy = False

    def show_error(self, error):
        with self.lock:
            self.message = "Search not started"
            self.error = error

    def set_notice(self, message):
        with self.lock:
            if not self.busy:
                self.message = message
                self.error = ""

    def reset_camera(self, camera_index):
        with self.lock:
            self.request_id += 1
            self.items = []
            self.message = f"Camera {camera_index}"
            self.error = ""
            self.busy = False
            self.snapshot = None

    def view(self):
        with self.lock:
            snapshot = None if self.snapshot is None else self.snapshot.copy()
            return list(self.items), self.error or self.message, self.busy, snapshot
