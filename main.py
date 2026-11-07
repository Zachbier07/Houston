"""Launcher for the webcam object finder."""
import argparse
import os
import sys


def load_api_key():
    """Read the key from the environment or ignored local key file."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return api_key
    key_file = os.path.join(os.path.dirname(__file__), "api_key.txt")
    try:
        with open(key_file, encoding="utf-8") as key_source:
            for line in key_source:
                candidate = line.strip()
                if candidate and not candidate.startswith("#"):
                    return candidate
    except OSError:
        pass
    return ""


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
    from app.camera_app import CameraApp, gui_startup_error, open_camera
    from app.state import AnalysisState
    from app.voice_listener import VoiceListener

    gui_error = gui_startup_error(cv2)
    if gui_error:
        print(gui_error, file=sys.stderr)
        return 2
    api_key = load_api_key()
    if not api_key:
        print("Put your Gemini API key in api_key.txt or set GEMINI_API_KEY.")
        return 2

    camera, camera_index = open_camera(cv2, args.camera)
    if camera is None:
        print(f"Could not open webcam {camera_index}.")
        return 1

    state = AnalysisState()
    analyzer = GeminiAnalyzer(create_client(api_key, genai, types), Image, types, state)
    app = CameraApp(cv2, camera, camera_index, analyzer, state)
    voice_listener = VoiceListener(app.handle_voice_command, state.set_notice)
    app.voice_listener = voice_listener
    try:
        app.run()
    except cv2.error:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
