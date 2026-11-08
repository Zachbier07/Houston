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
VOICE_OBJECT_CORRECTIONS = {"players": "pliers"}


# Words that make a request a question even when it starts with "find" / "where".
QUESTION_CUE = re.compile(
    r"\b(?:what|how|which|why|able to|should i|can i|could i|do i|where do|where should|"
    r"explain|help me (?:build|make|wire|use|measure))\b", re.IGNORECASE)


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
    # Vosk sometimes inserts a past-tense verb after a spoken "find", e.g.
    # "find my pliers" can become "find found my players". Once the command
    # prefix is stripped, discard that stray word.
    text = re.sub(r"^found\s+(?=(?:my|the|a|an)\s+)\s*", "", text,
                  flags=re.IGNORECASE)
    text = REMOVE_LEADING_DETERMINERS.sub("", text)
    text = REMOVE_TRAILING_POLITENESS.sub("", text).strip(" ,.!?\t\n")
    text = VOICE_OBJECT_CORRECTIONS.get(text.casefold(), text)
    return text


def command_after_wake_word(transcript):
    """Return (wake_word_seen, target_or_none) from one finalized utterance."""
    match = re.search(r"\bhouston\b", transcript, flags=re.IGNORECASE)
    if not match:
        return False, None
    return True, extract_command_target(transcript[match.end():])


class VoiceListener:
    """Continuously transcribes microphone speech locally and signals commands."""

    def __init__(self, on_command, on_status, on_greeting=None, on_question=None):
        self.on_command = on_command
        self.on_status = on_status
        self.on_greeting = on_greeting
        self.on_question = on_question
        self.stop_event = threading.Event()
        self.thread = None
        self.ignore_reply_until = 0.0
        configured_device = os.environ.get("HOUSTON_MIC_DEVICE", "").strip()
        self.device_index = int(configured_device) if configured_device else None

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

    @staticmethod
    def input_devices():
        """Return available microphone IDs and names for the UI selector."""
        import sounddevice as sd
        devices = [(index, device.get("name", f"Input {index}"))
                   for index, device in enumerate(sd.query_devices())
                   if int(device.get("max_input_channels", 0) or 0) > 0]
        try:
            default_index = sd.default.device[0]
            if default_index is not None and int(default_index) >= 0:
                info = sd.query_devices(int(default_index), "input")
                if all(index != int(default_index) for index, _name in devices):
                    devices.insert(0, (int(default_index),
                                       f"System default: {info.get('name', 'Microphone')}"))
        except Exception:
            pass
        if not devices:
            try:
                info = sd.query_devices(kind="input")
                devices.append((None, f"System default: {info.get('name', 'Microphone')}"))
            except Exception:
                pass
        return devices

    def set_device(self, device_index):
        """Switch microphone by restarting the offline recognition stream."""
        self.device_index = device_index
        self.stop()
        self.start()

    def submit_text(self, text):
        """Route typed chat input through the same search/question intent logic."""
        text = " ".join(text.split()).strip()
        if not text:
            return
        text = re.sub(r"^houston[,:]?\s*", "", text, flags=re.IGNORECASE)
        if self._is_greeting(text):
            self._greet()
        else:
            self._handle_request(text, text, log_user=False)

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
            device_index = self.device_index
            device = sd.query_devices(device_index, "input")
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
            print(f"Voice listener active on microphone '{device['name']}' "
                  f"at {sample_rate} Hz. Set HOUSTON_VOICE_DEBUG=1 to see transcripts.",
                  flush=True)
            with sd.RawInputStream(samplerate=sample_rate, blocksize=4000,
                                   device=device_index, dtype="int16", channels=1,
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
                        if os.environ.get("HOUSTON_VOICE_DEBUG") == "1":
                            print(f"Vosk heard: {transcript}", flush=True)
                        wake_seen, target = command_after_wake_word(transcript)
                        if wake_seen:
                            if self._is_greeting(target):
                                self._greet()
                                armed_until = 0.0
                            elif target:
                                self._handle_request(target, transcript)
                                armed_until = 0.0
                            else:
                                armed_until = time.monotonic() + ARM_SECONDS
                                self._greet("Houston heard; I'm listening")
                        elif time.monotonic() < armed_until:
                            if (time.monotonic() < self.ignore_reply_until
                                    and self._is_greeting(transcript)):
                                continue
                            if self._is_greeting(transcript):
                                self._greet()
                            else:
                                self._handle_request(transcript, transcript)
                            armed_until = 0.0
                        elif time.monotonic() >= armed_until:
                            self.on_status("Listening for 'Houston' (offline mic recognition)")
        except Exception as exc:
            message = f"Voice input unavailable: {exc}"
            print(message, file=sys.stderr, flush=True)
            self.on_status(message)

    def _dispatch(self, target, transcript):
        print(f"Wake word accepted. Search request: {transcript}", flush=True)
        self.on_status(f"Houston heard: searching for {target[:40]}")
        self.on_command(target)

    def _handle_request(self, text, transcript, log_user=True):
        if log_user:
            self.on_status(f"Heard: {transcript}")
        if self._is_object_search(text):
            target = extract_command_target(text)
            if target:
                self._dispatch(target, transcript)
            return
        if self._is_question(text) and self.on_question:
            question = " ".join(text.split())
            print(f"Voice question: {question}", flush=True)
            self.on_status("Houston is thinking...")
            self.on_question(question)
            return
        target = extract_command_target(text)
        if target:
            self._dispatch(target, transcript)
        else:
            self.on_status("I didn't catch an object name. Say Houston, then what to find.")

    @staticmethod
    def _is_greeting(text):
        if not text:
            return False
        return bool(re.search(
            r"\b(?:say hi|say hello|hello|hi|hey|how are you|what's up|good morning|good afternoon)\b",
            text, flags=re.IGNORECASE))

    @staticmethod
    def _is_question(text):
        text = text.strip()
        return bool(re.match(
            r"^(?:what|who|why|how|which|when|explain|describe|tell me|show me|"
            r"where (?:do|does|should|can|could|would|is the best)|"
            r"can i|could i|can you|could you|would you|do you|is there|are there|should i)\b",
            text, flags=re.IGNORECASE)) or "?" in text or bool(QUESTION_CUE.search(text))

    @staticmethod
    def _is_object_search(text):
        # "find based off these items what I can build" is a question, not a search
        if QUESTION_CUE.search(text):
            return False
        return bool(re.match(
            r"^(?:please\s+)?(?:(?:can|could|would) you\s+)?"
            r"(?:please\s+)?(?:find|look for|search for|locate|spot)\b|"
            r"^(?:where is|where's|where are|where did i (?:leave|put|last see))\b",
            text.strip(), flags=re.IGNORECASE))

    def _greet(self, status="Hi! What should I look for?"):
        self.on_status(status)
        self.ignore_reply_until = time.monotonic() + 2.5
        if self.on_greeting:
            self.on_greeting("Hi!")
