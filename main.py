"""Launcher for the webcam object finder."""
import argparse
import os
import sys


def load_api_keys():
    """Read Gemini then ElevenLabs keys from env vars or api_key.txt."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    key_file = os.path.join(os.path.dirname(__file__), "api_key.txt")
    try:
        with open(key_file, encoding="utf-8") as key_source:
            file_keys = [line.strip() for line in key_source
                         if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        file_keys = []
    if not gemini_key and file_keys:
        gemini_key = file_keys[0]
    if not elevenlabs_key and len(file_keys) > 1:
        elevenlabs_key = file_keys[1]
    return gemini_key, elevenlabs_key


def main():
    parser = argparse.ArgumentParser(
        description="Capture a webcam image and list objects with Gemini.")
    parser.add_argument("--camera", type=int, default=0,
                        help="webcam index to open first")
    args = parser.parse_args()

    try:
        import cv2
        from PIL import Image
        from google import genai
        from google.genai import types
    except ImportError as exc:
        print(f"Missing dependency: {getattr(exc, 'name', None) or exc}")
        print("Install dependencies with: python -m pip install -r requirements.txt")
        return 2

    from app.analyzer import GeminiAnalyzer, create_client
    from app.camera_app import CameraApp, gui_startup_error, open_available_camera
    from app.state import AnalysisState
    from app.voice_listener import VoiceListener

    gui_error = gui_startup_error(cv2)
    if gui_error:
        print(gui_error, file=sys.stderr)
        return 2
    gemini_key, elevenlabs_key = load_api_keys()
    if not gemini_key:
        print("Put your Gemini API key first in api_key.txt or set GEMINI_API_KEY.")
        return 2

    camera, camera_index = open_available_camera(cv2, args.camera)
    if camera is None:
        print(f"Could not open a webcam (tried indices {args.camera} and 0-9).")
        if sys.platform.startswith("linux"):
            print("Check that /dev/video* exists, your user can access the device "
                  "(often the video group), and another app is not using the webcam.")
        print("You can select the first camera to try with: python main.py --camera N")
        return 1

    state = AnalysisState()
    from app.speaker import ElevenLabsSpeaker

    speaker = ElevenLabsSpeaker(elevenlabs_key) if elevenlabs_key else None
    if speaker is None:
        print("ElevenLabs key not found; voice replies are disabled. Add it as the second key in api_key.txt.",
              file=sys.stderr)
    analyzer = GeminiAnalyzer(create_client(gemini_key, genai, types), Image, types,
                              state, speaker=speaker)
    app = CameraApp(cv2, camera, camera_index, analyzer, state)
    voice_listener = VoiceListener(
        app.handle_voice_command,
        state.set_notice,
        on_greeting=(lambda reply: speaker.speak(reply))
        if speaker else None,
        on_question=app.handle_voice_question,
    )
    app.voice_listener = voice_listener
    try:
        app.run()
    except cv2.error:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
