"""Always-on, offline wake-word and voice-command listener using Vosk."""
import json
import os
import queue
import re
import sys
import threading
import time


WAKE_WORD = "houston"
ARM_SECONDS = 8
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", MODEL_NAME)

# These patterns turn normal spoken requests into a compact object-name phrase.
COMMAND_PREFIXES = (
    r"^(?:please\s+)?(?:can you|could you|would you)\s+",
    r"^(?:please\s+)?(?:i am trying to find|i'm trying to find|i want you to find)\s+",
    r"^(?:please\s+)?(?:help me\s+)?(?:look for|find|search for|locate|spot)\s+",
    r"^(?:please\s+)?(?:tell me where|where can i find|where did i (?:leave|put|last see)|where is|where's|where are)\s+",
    r"^(?:please\s+)?(?:i am looking for|i'm looking for|i need to find|i need|i am searching for|i'm searching for)\s+",
    r"^(?:please\s+)?(?:show me)\s+",
)
REMOVE_LEADING_DETERMINERS = re.compile(r"^(?:(?:my|the|a|an|some)\s+)+", re.IGNORECASE)
REMOVE_TRAILING_POLITENESS = re.compile(r"\s+(?:please|for me|thanks|thank you)[.!?]*$", re.IGNORECASE)


def extract_command_target(words):
    """Strip common conversational wording while retaining the requested item."""
    text = " ".join(words.split()).strip(" ,.!?\t\n")
    text = re.sub(r"^(?:please\s+)+", "", text, flags=re.IGNORECASE)
    for _ in range(4):
        previous = text
        for prefix in COMMAND_PREFIXES:
            text = re.sub(prefix, "", text, count=1, flags=re.IGNORECASE)
            if text != previous:
                break
        if text == previous:
            break
    text = REMOVE_LEADING_DETERMINERS.sub("", text)
    text = REMOVE_TRAILING_POLITENESS.sub("", text).strip(" ,.!?\t\n")
    return text


def command_after_wake_word(transcript):
    """Return (wake_word_seen, target_or_none) from one finalized utterance."""
    match = re.search(r"\bhouston\b", transcript, flags=re.IGNORECASE)
    if not match:
        return False, None
    return True, extract_command_target(transcript[match.end():])


class VoiceListener:
    """Continuously transcribes microphone speech locally and signals commands."""

    def __init__(self, on_command, on_status):
        self.on_command = on_command
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._listen, name="HoustonVoiceListener",
                                      daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def _listen(self):
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer, Model, SetLogLevel
            if not os.path.isdir(MODEL_PATH):
                raise RuntimeError(
                    f"Vosk model missing at {MODEL_PATH}. Run: "
                    "python -m app.download_voice_model")

            SetLogLevel(-1)
            model = Model(MODEL_PATH)
            device = sd.query_devices(kind="input")
            sample_rate = int(device["default_samplerate"])
            recognizer = KaldiRecognizer(model, sample_rate)
            audio_queue = queue.Queue(maxsize=32)
            armed_until = 0.0

            def on_audio(indata, frames, timing, status):
                if status:
                    print(f"Microphone status: {status}", file=sys.stderr, flush=True)
                try:
                    audio_queue.put_nowait(bytes(indata))
                except queue.Full:
                    # Keep the recognizer near real time instead of building latency.
                    try:
                        audio_queue.get_nowait()
                        audio_queue.put_nowait(bytes(indata))
                    except queue.Empty:
                        pass

            self.on_status("Listening for 'Houston' (offline mic recognition)")
            with sd.RawInputStream(samplerate=sample_rate, blocksize=4000,
                                   device=None, dtype="int16", channels=1,
                                   callback=on_audio):
                while not self.stop_event.is_set():
                    try:
                        audio = audio_queue.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(audio):
                        transcript = json.loads(recognizer.Result()).get("text", "").strip()
                        if not transcript:
                            continue
                        wake_seen, target = command_after_wake_word(transcript)
                        if wake_seen:
                            if target:
                                self._dispatch(target, transcript)
                                armed_until = 0.0
                            else:
                                armed_until = time.monotonic() + ARM_SECONDS
                                self.on_status("Houston heard; say what to look for")
                        elif time.monotonic() < armed_until:
                            target = extract_command_target(transcript)
                            if target:
                                self._dispatch(target, transcript)
                            armed_until = 0.0
                        elif time.monotonic() >= armed_until:
                            self.on_status("Listening for 'Houston' (offline mic recognition)")
        except Exception as exc:
            message = f"Voice input unavailable: {exc}"
            print(message, file=sys.stderr, flush=True)
            self.on_status(message)

    def _dispatch(self, target, transcript):
        print(f"Voice command: {transcript}", flush=True)
        self.on_status(f"Houston heard: searching for {target[:40]}")
        self.on_command(target)
