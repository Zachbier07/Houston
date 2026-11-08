"""Brief ElevenLabs text-to-speech replies."""
import json
import os
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
MODEL_ID = "eleven_flash_v2_5"
OUTPUT_FORMAT = "pcm_24000"


class ElevenLabsSpeaker:
    """Generate and play short speech using the user's ElevenLabs API key."""

    def __init__(self, api_key):
        self.api_key = api_key.strip()
        self._lock = threading.Lock()

    def speak(self, text):
        """Speak a short phrase in a worker so callers stay responsive."""
        text = " ".join(text.split())[:160]
        if not text:
            return
        threading.Thread(target=self._speak, args=(text,), daemon=True,
                         name="HoustonSpeechOutput").start()

    def _speak(self, text):
        try:
            import numpy as np
            import sounddevice as sd

            request = Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
                f"?output_format={OUTPUT_FORMAT}",
                data=json.dumps({"text": text, "model_id": MODEL_ID}).encode("utf-8"),
                headers={"xi-api-key": self.api_key,
                         "Content-Type": "application/json",
                         "Accept": "audio/pcm"},
                method="POST",
            )
            with urlopen(request, timeout=20) as response:
                audio = response.read()
            samples = np.frombuffer(audio, dtype="<i2")
            if samples.size:
                with self._lock:
                    sd.play(samples, samplerate=24000, blocking=True)
        except HTTPError as exc:
            print(f"ElevenLabs speech error (HTTP {exc.code}). Check the API key and account credits.",
                  file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"ElevenLabs speech error: {exc}", file=sys.stderr, flush=True)
