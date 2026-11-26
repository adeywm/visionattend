"""Read one camera stream. Receive its URL on stdin so credentials stay out of argv."""
import base64
import http.client
import os
import struct
import sys
from time import monotonic
from urllib.parse import unquote, urlsplit

import cv2
import numpy as np

from vision_models import MAX_EDGE, MAX_FRAME

PACKET = struct.Struct(">IfB")
MAX_INPUT = 4 * 1024 * 1024


def rtsp_frames(url):
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    camera = cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5500, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000,
    ])
    try:
        while camera.isOpened():
            ok, frame = camera.read()
            if not ok or frame is None:
                break
            yield frame
    finally:
        camera.release()


def mjpeg_frames(parsed):
    # Connect directly to the validated IP and reject redirects.
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=4)
    headers = {"Accept": "multipart/x-mixed-replace"}
    if parsed.username is not None:
        credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200 or "multipart/x-mixed-replace" not in response.headers.get("Content-Type", "").lower():
            return
        buffer = bytearray()
        while block := response.read1(16384):
            buffer.extend(block)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    if len(buffer) > MAX_INPUT:
                        return
                    break
                if end - start > MAX_INPUT:
                    return
                encoded = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                frame = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    yield frame
    finally:
        connection.close()


def stream():
    url = sys.stdin.buffer.read(8192).decode("utf-8")
    parsed = urlsplit(url)
    frames = rtsp_frames(url) if parsed.scheme == "rtsp" else mjpeg_frames(parsed)
    interval_start = monotonic()
    count = 0
    fps = 0.0
    last_sent = float("-inf")
    for frame in frames:
        now = monotonic()
        count += 1
        if now - interval_start >= 1:
            fps = min(240.0, count / (now - interval_start))
            interval_start, count = now, 0
        if now - last_sent < 0.095:
            continue
        height, width = frame.shape[:2]
        scale = min(1, MAX_EDGE / max(height, width))
        if scale < 1:
            frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
        if min(frame.shape[:2]) < 100:
            continue
        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if not ok or jpeg.size > MAX_FRAME:
            continue
        sys.stdout.buffer.write(PACKET.pack(5 + jpeg.size, fps, 0))
        sys.stdout.buffer.write(jpeg.tobytes())
        sys.stdout.buffer.flush()
        last_sent = now


if __name__ == "__main__":
    stream()
