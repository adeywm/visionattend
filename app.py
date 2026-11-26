"""VisionAttend's desktop UI, REST API, and live verification endpoint."""
import asyncio
import csv
import io
import json
import os
import re
import sqlite3
import struct
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from time import monotonic
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, model_validator
from starlette.concurrency import run_in_threadpool

from vision_attendance import LiveAttendance
from vision_db import Store
from vision_ip_camera import IPCameras
from vision_models import MAX_FRAME, FaceEngine
from vision_phone import PhoneRelay
from vision_sessions import MAX_SESSIONS, Enrollments

DIST = Path(__file__).resolve().parent / "frontend" / "dist"
PAGES = {"register", "attendance", "users", "records", "settings"}
PUBLIC_ID = re.compile(r"VA-[0-9]{6,16}")
FRAME_HEADER = struct.Struct(">I")


class EnrollmentInput(BaseModel):
    full_name: str = Field(default="", max_length=100)
    email: EmailStr | None = Field(default=None, max_length=254)
    consent: bool
    public_id: str | None = Field(default=None, max_length=19)

    @model_validator(mode="after")
    def validate_enrollment(self):
        self.full_name = self.full_name.strip()
        if not self.consent:
            raise ValueError("Biometric consent is required.")
        if self.public_id:
            if not PUBLIC_ID.fullmatch(self.public_id):
                raise ValueError("Invalid profile ID.")
        elif not self.full_name or not self.email:
            raise ValueError("Full name and email are required.")
        return self


class Preferences(BaseModel):
    threshold: float = Field(ge=0, le=1)
    stable_frames: int = Field(ge=2, le=8, strict=True)


def csv_cell(value):
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def filters(
    start: date | None = None, end: date | None = None,
    public_id: Annotated[str, Query(max_length=19)] = "",
    search: Annotated[str, Query(max_length=100)] = "",
):
    if start and end and start > end:
        raise HTTPException(400, "Start date must be before or equal to end date.")
    return start.isoformat() if start else None, end.isoformat() if end else None, public_id, search


def create_app(store=None, engine=None, phone=None, clock=monotonic, desktop_port=8000):
    store = store or Store()
    engine = engine or FaceEngine()
    phone = phone or PhoneRelay()
    hosts = {f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (desktop_port, 5173)}
    origins = {"http://" + host for host in hosts}
    zone = ZoneInfo(os.environ.get("VISIONATTEND_TIMEZONE", "Asia/Kolkata"))
    enrollments = Enrollments(store, engine, clock)
    ip_cameras = IPCameras()
    terminals = 0

    @asynccontextmanager
    async def lifespan(_):
        await run_in_threadpool(store.initialize)
        await run_in_threadpool(engine.load)

        async def sweep():
            while True:
                await asyncio.sleep(5)
                await run_in_threadpool(enrollments.prune)
                phone.prune()

        cleanup = asyncio.create_task(sweep())
        try:
            yield
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup

    app = FastAPI(title="VisionAttend", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def desktop_boundary(request: Request, call_next):
        host = request.headers.get("host")
        origin = request.headers.get("origin")
        if host not in hosts or (origin is not None and origin != f"http://{host}"):
            return JSONResponse({"detail": "Only the local VisionAttend origin is allowed."}, status_code=403)
        if request.method == "POST" and request.url.path.endswith("/sample"):
            length = request.headers.get("content-length")
            if length is None or not length.isdecimal():
                return JSONResponse({"detail": "An upload length is required."}, status_code=411)
            if int(length) > MAX_FRAME + 4096:
                return JSONResponse({"detail": "Camera frame exceeds the upload limit."}, status_code=413)
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.Error)
    async def database_error(_, __):
        return JSONResponse({"detail": "Could not access the local database. Try again."}, status_code=503)

    @app.get("/api/health")
    def health():
        return {"models_ready": engine.ready, "phone_enabled": phone.origin is not None, "timezone": zone.key}

    @app.get("/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title="VisionAttend API",
            swagger_css_url="/docs-assets/swagger-ui.css", swagger_js_url="/docs-assets/swagger-ui-bundle.js",
            swagger_favicon_url="/docs-assets/favicon-32x32.png",
            swagger_ui_parameters={"validatorUrl": None},
        )

    @app.get("/docs-assets/{name}", include_in_schema=False)
    def docs_asset(name: str):
        if name not in {"swagger-ui.css", "swagger-ui-bundle.js", "favicon-32x32.png"}:
            raise HTTPException(404, "Asset not found.")
        path = DIST / "docs-assets" / name
        if not path.is_file():
            path = DIST.parent / "node_modules" / "swagger-ui-dist" / name
        if not path.is_file():
            raise HTTPException(503, "Install the frontend dependencies to use Swagger UI.")
        return FileResponse(path)

    @app.get("/api/dashboard")
    def overview():
        return store.dashboard(datetime.now(zone).date())

    @app.get("/api/users")
    def list_users(
        search: Annotated[str, Query(max_length=100)] = "",
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        size: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return store.users(search, page, size)

    @app.get("/api/users/{public_id}")
    def get_user(public_id: str):
        profile = store.profile(public_id.upper())
        if not profile:
            raise HTTPException(404, "No profile found for that ID.")
        return profile

    @app.delete("/api/users/{public_id}", status_code=204)
    def delete_user(public_id: str):
        if not store.delete(public_id.upper()):
            raise HTTPException(404, "No profile found for that ID.")

    @app.post("/api/enrollment/start", status_code=201)
    def start_enrollment(details: EnrollmentInput):
        return enrollments.start(details.full_name, (details.email or "").casefold(), details.public_id)

    @app.post("/api/enrollment/{session_id}/sample")
    async def enrollment_sample(session_id: str, image: UploadFile = File(...)):
        frame = await image.read(MAX_FRAME + 1)
        if len(frame) > MAX_FRAME:
            raise HTTPException(413, "Camera frame exceeds the upload limit.")
        if not frame:
            raise HTTPException(400, "No camera frame was received.")
        return await run_in_threadpool(enrollments.sample, session_id, frame)

    @app.post("/api/enrollment/{session_id}/complete", status_code=201)
    def complete_enrollment(session_id: str):
        return enrollments.complete(session_id)

    @app.delete("/api/enrollment/{session_id}", status_code=204)
    def cancel_enrollment(session_id: str):
        enrollments.cancel(session_id)

    def valid_socket(socket):
        origin = socket.headers.get("origin")
        return origin in origins and origin == "http://" + socket.headers.get("host", "")

    @app.websocket("/ws/ip-camera")
    async def ip_camera(socket: WebSocket):
        if not valid_socket(socket):
            await socket.close(code=1008)
            return
        await ip_cameras.serve(socket)

    @app.websocket("/ws/attendance")
    async def live_attendance(socket: WebSocket):
        nonlocal terminals
        if not valid_socket(socket):
            await socket.close(code=1008)
            return
        if terminals >= MAX_SESSIONS:
            await socket.close(code=1013)
            return
        await socket.accept()
        terminals += 1
        try:
            raw = await asyncio.wait_for(socket.receive_text(), 8)
            details = json.loads(raw)
            if len(raw) > 1024 or not isinstance(details, dict):
                raise ValueError()
            source_live = lambda: True
            if details.get("phone_pair") is not None:
                pair = phone.pairs.get(details["phone_pair"])
                if pair is None or details.get("viewer_key") != pair.viewer_key or not pair.viewer or not pair.sender:
                    await socket.send_json({"state": "error", "detail": "The phone camera is disconnected."})
                    return

                def source_live():
                    return not pair.closed and phone.clock() < pair.expires

            if not engine.ready:
                await socket.send_json({"state": "error", "detail": "Face models are unavailable."})
                return
            session = LiveAttendance(store, engine, zone, source_live=source_live, clock=clock)
            await socket.send_json({"state": "started"})
            last_seq = -1
            while source_live():
                packet = await asyncio.wait_for(socket.receive_bytes(), 8)
                if not (FRAME_HEADER.size < len(packet) <= FRAME_HEADER.size + MAX_FRAME):
                    await socket.close(code=1009)
                    return
                seq = FRAME_HEADER.unpack_from(packet)[0]
                if seq <= last_seq:
                    raise ValueError()
                last_seq = seq
                result = await run_in_threadpool(session.process, packet[FRAME_HEADER.size:])
                await socket.send_json({"state": "frame", "seq": seq, **result})
        except asyncio.TimeoutError:
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.send_json({"state": "error", "detail": "No live camera frames received. Restart the camera."})
        except sqlite3.Error:
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.send_json({"state": "error", "detail": "Could not save attendance. Restart the terminal."})
        except (ValueError, KeyError, TypeError):
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.send_json({"state": "error", "detail": "Invalid attendance session or frame."})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            terminals -= 1
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    @app.get("/api/attendance")
    def records(
        start: date | None = None, end: date | None = None,
        public_id: Annotated[str, Query(max_length=19)] = "",
        search: Annotated[str, Query(max_length=100)] = "",
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        size: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return store.records(*filters(start, end, public_id, search), page=page, size=size)

    @app.get("/api/attendance.csv")
    def export(
        start: date | None = None, end: date | None = None,
        public_id: Annotated[str, Query(max_length=19)] = "",
        search: Annotated[str, Query(max_length=100)] = "",
    ):
        report = store.records(*filters(start, end, public_id, search))
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Profile ID", "Name", "Attendance date", f"Check-in ({zone.key})", "Cosine similarity"])
        for row in report["items"]:
            checked = datetime.fromisoformat(row["checked_in_at"]).astimezone(zone).isoformat(timespec="seconds")
            writer.writerow([row["public_id"], csv_cell(row["full_name"]), row["attendance_date"], checked, row["similarity"]])
        return Response(output.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="attendance.csv"'
        })

    @app.get("/api/settings")
    def get_settings():
        return {**store.policy(), **health()}

    @app.patch("/api/settings")
    def save_settings(details: Preferences):
        return {**store.policy(details.threshold, details.stable_frames), **health()}

    @app.post("/api/phone/pairs", status_code=201)
    async def new_pair():
        return phone.new()

    @app.websocket("/ws/phone/view")
    async def view_phone(socket: WebSocket):
        if not valid_socket(socket):
            await socket.close(code=1008)
            return
        await phone.connect(socket, origins, sender=False)

    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{page}", include_in_schema=False)
    def frontend(page: str = ""):
        if page and page not in PAGES:
            raise HTTPException(404, "Page not found.")
        index = DIST / "index.html"
        if not index.is_file():
            return Response("React build missing. Run npm ci and npm run build in frontend.", status_code=503)
        return FileResponse(index)

    return app


app = create_app()
