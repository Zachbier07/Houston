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
SAMPLE_RATE = 24000


def output_device():
    """Which sound output to play on. The PortAudio default can be a raw HDMI
    device that rejects 24 kHz audio; the system sound server (PipeWire/Pulse)
    converts the rate and follows the desktop's chosen speaker. Override with
    HOUSTON_AUDIO_OUT=<device name or number>."""
    import sounddevice as sd
    override = os.environ.get("HOUSTON_AUDIO_OUT", "").strip()
    if override:
        return int(override) if override.isdigit() else override
    names = [d["name"] for d in sd.query_devices()]
    for preferred in ("pipewire", "pulse", "default"):
        if preferred in names:
            return preferred
    return None   # PortAudio's default


class ElevenLabsSpeaker:
    """Generate and play short speech using the user's ElevenLabs API key."""

    def __init__(self, api_key):
        self.api_key = api_key.strip()
        self._lock = threading.Lock()

    def speak(self, text):
        """Speak a short phrase in a worker so callers stay responsive."""
        # Speak the complete answer returned by the model.
        text = " ".join(text.split())
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
                    sd.play(samples, samplerate=SAMPLE_RATE, blocking=True, device=output_device())
        except HTTPError as exc:
            print(f"ElevenLabs speech error (HTTP {exc.code}). Check the API key and account credits.",
                  file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"ElevenLabs speech error: {exc}", file=sys.stderr, flush=True)
