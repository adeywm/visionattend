"""Bounded JPEG decoding and shared YuNet/SFace inference."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from time import perf_counter

import cv2
import numpy as np
from PIL import Image

MODEL_DIR = Path(__file__).resolve().parent / "models"
DETECTOR = "face_detection_yunet_2023mar.onnx"
MODEL_VERSION = "face_recognition_sface_2021dec"
RECOGNIZER = MODEL_VERSION + ".onnx"
MAX_FRAME = 512 * 1024
MAX_EDGE = 640
MIN_FACE = 72
MAX_FACES = 8


@dataclass
class Analysis:
    quality: str
    latency_ms: float
    box: list[float] | None = None
    detection_confidence: float | None = None
    embedding: np.ndarray | None = None


@dataclass
class FrameAnalysis:
    quality: str
    latency_ms: float
    faces: list[Analysis]


def normalize(vector):
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = np.linalg.norm(vector)
    if vector.size != 128 or not np.isfinite(norm) or norm <= 0:
        raise ValueError("Invalid SFace embedding.")
    return vector / norm


def cosine(a, b):
    return float(np.clip(np.dot(a, b), -1, 1))


def jpeg_dimensions(frame):
    if not frame or len(frame) > MAX_FRAME:
        raise ValueError("Invalid frame size.")
    try:
        with Image.open(BytesIO(frame), formats=["JPEG"]) as source:
            width, height = source.size
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Expected a valid JPEG frame.") from exc
    if not (100 <= width <= MAX_EDGE and 100 <= height <= MAX_EDGE):
        raise ValueError("Frame dimensions are unsupported.")
    return width, height


class FaceEngine:
    def __init__(self, directory=MODEL_DIR):
        self.directory = directory
        self.lock = Lock()
        self.detector = None
        self.recognizer = None
        self.ready = False

    def load(self):
        with self.lock:
            try:
                self.detector = cv2.FaceDetectorYN.create(str(self.directory / DETECTOR), "", (640, 480), 0.9)
                self.recognizer = cv2.FaceRecognizerSF.create(str(self.directory / RECOGNIZER), "")
                self.detector.detect(np.zeros((480, 640, 3), dtype=np.uint8))
                normalize(self.recognizer.feature(np.zeros((112, 112, 3), dtype=np.uint8)))
            except (cv2.error, ValueError):
                self.detector = self.recognizer = None
            self.ready = self.detector is not None and self.recognizer is not None

    def analyze(self, frame):
        result = self._analyze_frame(frame, single=True)
        if result.faces:
            face = result.faces[0]
            face.latency_ms = result.latency_ms
            return face
        return Analysis(result.quality, result.latency_ms)

    def analyze_all(self, frame):
        return self._analyze_frame(frame, single=False)

    def _analyze_frame(self, frame, single):
        start = perf_counter()

        def finish(quality, faces=()):
            return FrameAnalysis(quality, round((perf_counter() - start) * 1000, 1), list(faces))

        if not self.ready:
            return finish("models_unavailable")
        try:
            width, height = jpeg_dimensions(frame)
            image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
            if image is None or image.shape[:2] != (height, width):
                return finish("invalid_frame")
        except (ValueError, cv2.error):
            return finish("invalid_frame")
        try:
            with self.lock:
                self.detector.setInputSize((width, height))
                _, faces = self.detector.detect(image)
                if faces is None or not len(faces):
                    return finish("no_face")
                if single and len(faces) != 1:
                    return finish("multiple_faces")
                if len(faces) > MAX_FACES:
                    return finish("too_many_faces")
                return finish("faces", [self._extract(image, face, width, height) for face in faces])
        except (cv2.error, ValueError):
            return finish("inference_error")

    def _extract(self, image, face, width, height):
        x, y, w, h = (float(value) for value in face[:4])
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(width, int(x + w)), min(height, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return Analysis("no_face", 0)
        box = [x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height]
        confidence = float(face[14])
        if min(x1 - x0, y1 - y0) < MIN_FACE:
            return Analysis("face_too_small", 0, box, confidence)
        crop = image[y0:y1, x0:x1]
        gray = cv2.cvtColor(cv2.resize(crop, (112, 112)), cv2.COLOR_BGR2GRAY)
        if cv2.Laplacian(gray, cv2.CV_64F).var() < 45:
            return Analysis("blurry", 0, box, confidence)
        try:
            aligned = self.recognizer.alignCrop(image, face)
            return Analysis("usable", 0, box, confidence, normalize(self.recognizer.feature(aligned)))
        except (cv2.error, ValueError):
            return Analysis("inference_error", 0, box, confidence)
