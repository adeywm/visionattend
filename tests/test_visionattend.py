import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

from app import create_app
from vision_attendance import LiveAttendance
from vision_db import Store
from vision_ip_camera import camera_url
from vision_models import Analysis, FaceEngine, FrameAnalysis, MAX_EDGE, MODEL_VERSION, jpeg_dimensions, normalize
from vision_phone import PhoneRelay, serve_phone

DESKTOP = "http://127.0.0.1:8000"
ORIGIN = {"origin": DESKTOP, "host": urlsplit(DESKTOP).netloc}
PHONE = "https://192.168.1.5:8443"
GOOD = np.eye(128, dtype=np.float32)[0]
BAD = np.eye(128, dtype=np.float32)[1]


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def step(self, interval=0.7):
        self.now += interval


class FakeEngine:
    ready = True

    def load(self):
        pass

    def analyze(self, frame):
        if frame in (b"no_face", b"multiple_faces", b"blurry"):
            return Analysis(frame.decode(), 8)
        vector = BAD if frame == b"mismatch" else GOOD
        return Analysis("usable", 8, [0.2, 0.2, 0.5, 0.5], 0.95, vector)


    def analyze_all(self, frame):
        faces = {
            b"multi": [GOOD, BAD],
            b"duplicate": [GOOD, GOOD],
            b"unknown": [np.eye(128, dtype=np.float32)[2]],
            b"no_face": [],
        }.get(frame, [GOOD])
        return FrameAnalysis("faces" if faces else "no_face", 8, [
            Analysis("usable", 0, [0.1 + i * 0.45, 0.1, 0.4, 0.5], 0.95, vector)
            for i, vector in enumerate(faces)
        ])


@pytest.fixture
def app_client(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "new.db")
    with TestClient(create_app(store, FakeEngine(), clock=clock), base_url=DESKTOP) as client:
        yield client, store, clock


def start(client, email="Ada@EXAMPLE.com", name="Ada Lovelace", consent=True):
    return client.post("/api/enrollment/start", json={"full_name": name, "email": email, "consent": consent})


def sample(client, session, value):
    return client.post(f"/api/enrollment/{session}/sample", files={"image": ("frame.jpg", value, "image/jpeg")})


def finish(client, clock, session):
    for i in range(5):
        clock.step()
        assert sample(client, session, bytes([i + 1])).json()["count"] == i + 1
    response = client.post(f"/api/enrollment/{session}/complete")
    assert response.status_code == 201, response.text
    return response.json()


def enroll(client, clock, email="Ada@EXAMPLE.com", name="Ada Lovelace"):
    response = start(client, email, name)
    assert response.status_code == 201, response.text
    return finish(client, clock, response.json()["session_id"])


def frame(ws, sequence, contents=b"ok"):
    ws.send_bytes(struct.pack(">I", sequence) + contents)
    return ws.receive_json()


def start_terminal(ws):
    ws.send_json({})
    assert ws.receive_json() == {"state": "started"}


def test_invalid_and_cancelled_enrollment_never_creates_profile(app_client):
    client, _, clock = app_client
    assert start(client, email="invalid").status_code == 422
    assert start(client, consent=False).status_code == 422
    session = start(client).json()["session_id"]
    for bad in (b"no_face", b"multiple_faces", b"blurry"):
        result = sample(client, session, bad).json()
        assert (result["quality"], result["count"]) == (bad.decode(), 0)
    assert sample(client, session, b"usable").json()["count"] == 1
    clock.step()
    assert sample(client, session, b"usable").json()["quality"] == "change_pose"
    assert sample(client, session, b"mismatch").json()["quality"] == "different_face"
    assert client.post(f"/api/enrollment/{session}/complete").status_code == 409
    assert client.delete(f"/api/enrollment/{session}").status_code == 204
    assert sample(client, session, b"usable").status_code == 410
    assert client.get("/api/users").json() == {"items": [], "total": 0}
    expiring = start(client).json()["session_id"]
    clock.step(301)
    assert client.post(f"/api/enrollment/{expiring}/complete").status_code == 410


def test_enrollment_and_reenrollment_keep_identity(app_client):
    client, store, clock = app_client
    profile = enroll(client, clock)
    assert profile["public_id"] == "VA-000001"
    assert profile["email"] == "ada@example.com"
    assert profile["sample_count"] == 5
    assert "embedding" not in profile
    assert start(client, email="ADA@example.com").status_code == 409
    revision = store.template(profile["public_id"])["revision"]
    session = client.post("/api/enrollment/start", json={"public_id": profile["public_id"], "consent": True}).json()["session_id"]
    assert store.template(profile["public_id"])["revision"] == revision
    assert finish(client, clock, session) == profile
    assert store.template(profile["public_id"])["revision"] != revision
    assert client.get("/api/users").json()["total"] == 1


def test_stable_automatic_recognition_ignores_unknown_and_stale_frames(app_client):
    client, _, clock = app_client
    profile = enroll(client, clock)
    with client.websocket_connect("/ws/attendance", headers=ORIGIN) as ws:
        start_terminal(ws)
        assert frame(ws, 1)["faces"][0]["stable_count"] == 1
        unknown = frame(ws, 2, b"unknown")
        assert unknown["faces"][0]["state"] == "unknown"
        assert unknown["records"] == []
        assert frame(ws, 3)["faces"][0]["stable_count"] == 1
        assert frame(ws, 4)["faces"][0]["stable_count"] == 2
        clock.step(3)
        assert frame(ws, 5)["faces"][0]["stable_count"] == 1
        assert frame(ws, 6)["records"] == []
        final = frame(ws, 7)
        assert final["faces"][0]["state"] == "checked_in"
        assert final["faces"][0]["detection_confidence"] == 0.95
        assert final["records"][0]["public_id"] == profile["public_id"]
        assert frame(ws, 8)["records"] == []
    assert client.get("/api/attendance").json()["total"] == 1


def test_distinct_faces_check_in_together_and_duplicate_matches_abstain(app_client):
    client, store, clock = app_client
    first = enroll(client, clock)
    second = store.save_enrollment("Second", "second@example.com", BAD.tobytes(), 5, MODEL_VERSION)
    with client.websocket_connect("/ws/attendance", headers=ORIGIN) as ws:
        start_terminal(ws)
        for seq in range(1, 4):
            result = frame(ws, seq, b"duplicate")
        assert [face["state"] for face in result["faces"]] == ["ambiguous", "ambiguous"]
        assert not result["records"]
        for seq in range(4, 7):
            result = frame(ws, seq, b"multi")
        assert {row["public_id"] for row in result["records"]} == {first["public_id"], second["public_id"]}
    assert client.get("/api/attendance").json()["total"] == 2


def test_close_gallery_scores_are_rejected(app_client):
    client, store, clock = app_client
    enroll(client, clock)
    nearly_same = normalize(GOOD + BAD * 0.03)
    store.save_enrollment("Similar", "similar@example.com", nearly_same.tobytes(), 5, MODEL_VERSION)
    with client.websocket_connect("/ws/attendance", headers=ORIGIN) as ws:
        start_terminal(ws)
        for seq in range(1, 4):
            result = frame(ws, seq)
        assert result["faces"][0]["state"] == "ambiguous"
        assert result["records"] == []
    assert client.get("/api/attendance").json()["total"] == 0


def test_check_in_receipts_dashboard_filters_export_and_delete(app_client):
    client, _, clock = app_client
    ident = enroll(client, clock, name="=SUM(A1)")["public_id"]
    for expected in (True, False):
        with client.websocket_connect("/ws/attendance", headers=ORIGIN) as ws:
            start_terminal(ws)
            for seq in range(1, 4):
                result = frame(ws, seq)
            receipt = result["records"][0]
            assert receipt["created"] is expected
            if expected:
                timestamp = receipt["checked_in_at"]
            else:
                assert receipt["checked_in_at"] == timestamp
    assert client.get("/api/dashboard").json()["present"] == 1
    assert client.get("/api/attendance", params={"search": "SUM", "public_id": ident}).json()["total"] == 1
    assert client.get("/api/attendance", params={"start": "2099-01-01"}).json()["total"] == 0
    assert "'=SUM(A1)" in client.get("/api/attendance.csv", params={"public_id": ident}).text
    assert client.delete("/api/users/" + ident).status_code == 204
    assert client.get("/api/attendance").json()["total"] == 0


def test_profile_and_attendance_constraints_under_concurrency(tmp_path):
    store = Store(tmp_path / "race.db")
    store.initialize()

    def enroll_once(_):
        try:
            return store.save_enrollment("Ada", "ada@example.com", GOOD.tobytes(), 5, MODEL_VERSION)
        except sqlite3.IntegrityError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(enroll_once, range(4)))
        assert sum(isinstance(result, dict) for result in outcomes) == 1
        assert store.users("", 1, 20)["total"] == 1
        row = store.template("VA-000001")
        instant = datetime.now(timezone.utc)

        def mark(_):
            return store.mark(row["uuid"], row["revision"], "2026-11-26", 0.9, instant)

        results = list(pool.map(mark, range(4)))
        assert sum(record["created"] for record in results) == 1
        assert {record["checked_in_at"] for record in results} == {results[0]["checked_in_at"]}


def test_local_date_is_derived_from_saved_utc_instant(tmp_path):
    store = Store(tmp_path / "zone.db")
    store.initialize()
    profile = store.save_enrollment("Ada", "ada@example.com", GOOD.tobytes(), 5, MODEL_VERSION)
    instant = datetime(2026, 11, 26, 20, tzinfo=timezone.utc)
    service = LiveAttendance(store, FakeEngine(), ZoneInfo("Asia/Kolkata"), wall_clock=lambda: instant)
    for _ in range(3):
        result = service.process(b"ok")
    saved = result["records"][0]
    assert saved["public_id"] == profile["public_id"]
    assert saved["attendance_date"] == "2026-11-27"
    assert datetime.fromisoformat(saved["checked_in_at"]) == instant


def test_frame_limits_and_normalization():
    with pytest.raises(ValueError):
        normalize(np.zeros(128))
    for width in (99, MAX_EDGE + 1):
        image = BytesIO()
        Image.new("RGB", (width, 480)).save(image, "JPEG")
        with pytest.raises(ValueError):
            jpeg_dimensions(image.getvalue())
    with pytest.raises(ValueError):
        jpeg_dimensions(b"not an image")


def test_models_load_and_analyze_jpeg_when_installed():
    engine = FaceEngine()
    if not all((engine.directory / name).is_file() for name in (
        "face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"
    )):
        pytest.skip("Download models to run this smoke check.")
    engine.load()
    assert engine.ready
    frame = BytesIO()
    Image.new("RGB", (640, 480)).save(frame, "JPEG")
    assert engine.analyze(frame.getvalue()).quality == "no_face"


def test_phone_listener_isolated_and_relay_rejects_replay(tmp_path):
    clock = Clock()
    phone = PhoneRelay(PHONE, clock=clock)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>Companion</title>", encoding="utf-8")
    desktop = create_app(Store(tmp_path / "db.sqlite"), FakeEngine(), phone, clock)
    main = Starlette(routes=[Mount("/companion", serve_phone(phone, dist)), Mount("/", desktop)],
                     lifespan=desktop.router.lifespan_context)
    headers = {"origin": PHONE, "host": urlsplit(PHONE).netloc}
    with TestClient(main, base_url=DESKTOP) as client:
        assert client.get("/companion/phone", headers=headers).status_code == 200
        assert client.get("/companion/api/users", headers=headers).status_code == 404
        assert client.get("/companion/docs", headers=headers).status_code == 404
        assert client.get("/api/users", headers={"origin": PHONE}).status_code == 403
        pair = client.post("/api/phone/pairs").json()
        ident, secret = urlsplit(pair["url"]).fragment.split(".")
        image = BytesIO()
        Image.new("RGB", (640, 480)).save(image, "JPEG")
        payload = struct.pack(">fB", 29.5, 1) + image.getvalue()
        with client.websocket_connect("/ws/phone/view", headers=ORIGIN) as view:
            view.send_json({"pair_id": ident, "key": pair["viewer_key"]})
            with client.websocket_connect("/companion/ws/camera", headers=headers) as camera:
                camera.send_json({"pair_id": ident, "key": secret})
                assert camera.receive_json()["status"] == "connected"
                camera.send_bytes(payload)
                assert view.receive_json()["status"] == "connected"
                assert view.receive_bytes() == payload
                with pytest.raises(WebSocketDisconnect):
                    with client.websocket_connect("/companion/ws/camera", headers=headers) as replay:
                        replay.send_json({"pair_id": ident, "key": secret})
                        replay.receive_json()
            with pytest.raises(WebSocketDisconnect):
                view.receive_bytes()

        expired = client.post("/api/phone/pairs").json()
        ident, secret = urlsplit(expired["url"]).fragment.split(".")
        clock.step(121)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/companion/ws/camera", headers=headers) as camera:
                camera.send_json({"pair_id": ident, "key": secret})
                camera.receive_json()


@pytest.mark.parametrize("url", ["file:///tmp/camera", "rtsp://example.com/live", "rtsp://127.0.0.1/live", "http://169.254.169.254/live"])
def test_ip_camera_rejects_urls_outside_the_private_network(url):
    with pytest.raises(ValueError):
        camera_url(url)


def test_ip_camera_accepts_private_rtsp_and_mjpeg_urls():
    for url in ("rtsp://192.168.1.20:8554/live", "http://10.1.1.2:8080/video"):
        assert camera_url(url) == url
