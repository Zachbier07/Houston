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

# Speech recognition, two stages (tested on noisy spoken commands: Vosk small alone
# had 14% word errors and took ~1.1 s; Whisper base.en had 0% in ~0.45 s):
#   1. Vosk listens all the time, restricted to the wake word, so "Houston" is
#      recognized reliably and cheaply, and it marks where each utterance ends.
#   2. The audio of an utterance containing the wake word (or the next one after a
#      bare "Houston") is transcribed by Whisper for the actual request.
SAMPLE_RATE = 16000              # what both recognizers want; PipeWire resamples the mic
WAKE_GRAMMAR = ["houston", "hey houston", "okay houston", "hi houston", "[unk]"]
WHISPER_MODEL = os.environ.get("HOUSTON_WHISPER_MODEL", "base.en")
WHISPER_HINT = ("Houston, find the soldering iron, multimeter, breadboard, Red Bull, Monster, "
                "needle nose pliers, resistor kit, wire strippers, jumper wire, red probe.")
WAKE_SPELLINGS = re.compile(r"\b(?:huston|hueston|houstin|houstan|hughston|hewston|euston)\b",
                            re.IGNORECASE)
MAX_UTTERANCE_S = 10             # stop recording a request after this long
PREROLL_S = 2.0                  # audio kept from before the wake word was recognized
END_SILENCE_S = 0.8              # this much quiet after speech = you're done talking
QUIET_FACTOR = 2.5               # "quiet" = below this x the room's background level

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
    r"explain|help me (?:build|make|wire|use|measure)|plug|plugged|connect|hook up|wire up)\b", re.IGNORECASE)


INSTRUCTION_VERBS = {"read", "check", "count", "compare", "identify", "measure", "test", "list",
                     "name", "label", "help", "teach", "walk", "guide", "point", "summarize",
                     "translate", "spell", "solve", "calculate", "inspect", "verify", "rate"}
FIND_MAX_WORDS = 4   # a bare request this short ("pliers", "the red multimeter") is an object to find


STOP_WORDS = re.compile(r"^(?:please\s+)?(?:stop|cancel|shut up|be quiet|quiet|never ?mind|that's enough|enough|"
                        r"hold on|wait)\b", re.IGNORECASE)


def is_stop_request(text):
    """'stop', 'cancel', 'be quiet', 'never mind'... (said right after 'Houston')."""
    return bool(STOP_WORDS.match(text.strip(" ,.!?")))


def route_request(text):
    """'find' or 'question'. Find only when it clearly is one: search wording
    ("find the pliers", "where are my keys") or a short object name. Everything
    else ("read me the card", "count the batteries", "check my build") is a
    question; the question path can still point the laser at things."""
    if VoiceListener._is_object_search(text):
        return "find"
    if VoiceListener._is_question(text):
        return "question"
    words = re.findall(r"[A-Za-z0-9']+", text)
    if words and words[0].lower() in INSTRUCTION_VERBS:
        return "question"            # "check my build", "read the label", "count the cans"
    return "find" if len(words) <= FIND_MAX_WORDS else "question"


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

    def __init__(self, on_command, on_status, on_greeting=None, on_question=None, on_stop=None):
        self.on_stop = on_stop
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
            from faster_whisper import WhisperModel
            import numpy as np
            whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
            device_index = self.device_index
            device = sd.query_devices(device_index, "input")
            # 16 kHz: a third of the data of 48 kHz, so the mic doesn't overflow when
            # the app is busy. Fall back to the device's own rate if it refuses.
            sample_rate = SAMPLE_RATE
            try:
                sd.check_input_settings(device=device_index, samplerate=SAMPLE_RATE, channels=1,
                                        dtype="int16")
            except Exception:
                sample_rate = int(device["default_samplerate"])
            recognizer = KaldiRecognizer(model, sample_rate, json.dumps(WAKE_GRAMMAR))
            audio_queue = queue.Queue(maxsize=200)      # ~20 s of audio: transcribing never drops any
            chunk_s = 0.1
            armed_until = 0.0
            debug = os.environ.get("HOUSTON_VOICE_DEBUG") == "1"
            recent = []                                  # rolling pre-roll audio
            levels = []                                  # rolling loudness while idle -> room level
            capture = None                               # list of chunks while recording a request
            quiet_run = 0
            heard_speech = False

            def transcribe(chunks):
                pcm = np.frombuffer(b"".join(chunks), "<i2").astype(np.float32) / 32768
                if sample_rate != SAMPLE_RATE:           # Whisper needs 16 kHz
                    n = int(len(pcm) * SAMPLE_RATE / sample_rate)
                    pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
                started = time.monotonic()
                segments, _ = whisper.transcribe(pcm.astype(np.float32), language="en", beam_size=1,
                                                 initial_prompt=WHISPER_HINT, vad_filter=False)
                text = " ".join(seg.text for seg in segments).strip()
                if debug:
                    print(f"Whisper heard ({time.monotonic() - started:.2f} s): {text}", flush=True)
                return text

            def on_audio(indata, frames, timing, status):
                if status:
                    print(f"Microphone status: {status}", file=sys.stderr, flush=True)
                try:
                    audio_queue.put_nowait(bytes(indata))
                except queue.Full:
                    pass

            def handle(transcript, from_wake):
                """Same routing as before, on Whisper's text."""
                nonlocal armed_until
                # Whisper double-checks Vosk's wake word: Vosk's quick guesses sometimes
                # "hear" Houston in ordinary talk. Accept close spellings, ignore the rest.
                transcript = WAKE_SPELLINGS.sub("Houston", transcript)
                wake_seen, target = command_after_wake_word(transcript)
                if from_wake and not wake_seen:
                    if debug:
                        print(f"Ignored (no wake word in what Whisper heard): {transcript}", flush=True)
                    return
                if wake_seen:
                    if self._is_greeting(target):
                        self._greet()
                        armed_until = 0.0
                    elif target:
                        # Route on everything said after "Houston", not the version with
                        # "can you / please" stripped off (that turned "can you read me
                        # the card" into a search for "read me the card").
                        after = transcript[re.search(r"\bhouston\b", transcript, re.IGNORECASE).end():]
                        self._handle_request(after.strip(" ,.!?:;") or target, transcript)
                        armed_until = 0.0
                    else:
                        armed_until = time.monotonic() + ARM_SECONDS
                        self._greet("Houston heard; I'm listening")
                else:
                    if (time.monotonic() < self.ignore_reply_until
                            and self._is_greeting(transcript)):
                        return
                    if self._is_greeting(transcript):
                        self._greet()
                    else:
                        self._handle_request(transcript, transcript)
                    armed_until = 0.0

            self.on_status("Listening for 'Houston' (offline mic recognition)")
            print(f"Voice listener active on microphone '{device['name']}' at {sample_rate} Hz "
                  f"(wake word: Vosk, requests: Whisper {WHISPER_MODEL}).", flush=True)
            with sd.RawInputStream(samplerate=sample_rate, blocksize=int(sample_rate * chunk_s),
                                   device=device_index, dtype="int16", channels=1,
                                   latency="high", callback=on_audio):
                while not self.stop_event.is_set():
                    try:
                        audio = audio_queue.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    level = float(np.sqrt(np.mean(np.frombuffer(audio, "<i2").astype(np.float32) ** 2)))
                    room = float(np.median(levels)) if levels else 300.0
                    quiet = level < max(QUIET_FACTOR * room, 150.0)

                    if capture is not None:
                        # Recording a request: until enough quiet after speech, or too long.
                        capture.append(audio)
                        heard_speech = heard_speech or not quiet
                        quiet_run = quiet_run + 1 if quiet else 0
                        too_long = len(capture) * chunk_s > MAX_UTTERANCE_S
                        if (heard_speech and quiet_run * chunk_s >= END_SILENCE_S) or too_long:
                            chunks, capture, from_wake = capture, None, capture_from_wake
                            recognizer.Reset()
                            recent = []
                            transcript = transcribe(chunks)
                            if transcript:
                                handle(transcript, from_wake)
                        continue

                    # Idle: track the room level, keep a pre-roll, and spot the wake word.
                    recent = (recent + [audio])[-int(PREROLL_S / chunk_s):]
                    levels = (levels + [level])[-int(5 / chunk_s):]
                    armed = time.monotonic() < armed_until
                    woke = False
                    if recognizer.AcceptWaveform(audio):
                        woke = "houston" in json.loads(recognizer.Result()).get("text", "")
                    else:
                        woke = "houston" in json.loads(recognizer.PartialResult()).get("partial", "")
                    if woke or (armed and not quiet):
                        if debug:
                            print("Wake word heard; recording request" if woke
                                  else "Listening after 'Houston'", flush=True)
                        capture = list(recent) if woke else list(recent[-5:])
                        capture_from_wake = woke
                        heard_speech, quiet_run = True, 0
                        if woke:
                            recognizer.Reset()
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
        if is_stop_request(text):
            print(f"Stop requested: {transcript}", flush=True)
            if self.on_stop:
                self.on_stop()
            return
        if self._is_object_search(text):
            target = extract_command_target(text)
            if target:
                self._dispatch(target, transcript)
            return
        if route_request(text) == "question" and self.on_question:
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
