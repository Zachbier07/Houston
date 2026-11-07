"""Download the small English Vosk model used by the offline voice listener."""
from pathlib import Path
from urllib.request import urlretrieve
import zipfile


MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_ROOT / "models"


def main():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_DIR / MODEL_NAME
    if model_path.is_dir():
        print(f"Vosk model is already installed: {model_path}")
        return 0

    archive_path = MODEL_DIR / f"{MODEL_NAME}.zip"
    print(f"Downloading the Vosk English model (~40 MB) from {MODEL_URL}")
    try:
        urlretrieve(MODEL_URL, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                destination = (MODEL_DIR / entry.filename).resolve()
                if not destination.is_relative_to(MODEL_DIR.resolve()):
                    raise ValueError("Model archive contains an invalid path.")
            archive.extractall(MODEL_DIR)
    finally:
        archive_path.unlink(missing_ok=True)

    if not model_path.is_dir():
        raise RuntimeError("The archive downloaded, but the expected model folder is missing.")
    print(f"Vosk model installed: {model_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
