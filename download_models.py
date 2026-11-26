"""Download the OpenCV Zoo models and verify the exact ONNX artifacts."""
from hashlib import sha256
from urllib.request import Request, urlopen

from vision_models import DETECTOR, MODEL_DIR, RECOGNIZER

BASE = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
MODELS = {
    DETECTOR: ("face_detection_yunet", "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    RECOGNIZER: ("face_recognition_sface", "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}


def digest(path):
    result = sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(65536), b""):
            result.update(block)
    return result.hexdigest()


def main():
    MODEL_DIR.mkdir(exist_ok=True)
    for filename, (directory, expected) in MODELS.items():
        destination = MODEL_DIR / filename
        if not destination.is_file() or digest(destination) != expected:
            print(f"Downloading {filename}...", flush=True)
            temporary = destination.with_suffix(".download")
            try:
                request = Request(f"{BASE}/{directory}/{filename}", headers={"User-Agent": "VisionAttend"})
                length = 0
                with urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                    while block := response.read(65536):
                        length += len(block)
                        if length > 50 * 1024 * 1024:
                            raise ValueError("Model exceeds the expected download limit.")
                        output.write(block)
                if digest(temporary) != expected:
                    raise ValueError("Download failed checksum validation.")
                temporary.replace(destination)
            except (OSError, ValueError) as exc:
                raise SystemExit(f"Could not download {filename}: {exc}") from exc
            finally:
                temporary.unlink(missing_ok=True)
        print(f"Ready: {destination}")


if __name__ == "__main__":
    main()
