"""Local, transient phone camera relay. It has no database or admin routes."""
import asyncio
import ipaddress
import json
import math
import secrets
import struct
from dataclasses import dataclass, field
from time import monotonic

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from vision_models import MAX_FRAME, jpeg_dimensions

PAIR_TTL = 120
ACTIVE_TTL = 300
MAX_PAIRS = 8
HEADER = struct.Struct(">fB")
LAN_RANGES = [ipaddress.IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def private_address(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in network for network in LAN_RANGES):
        raise ValueError("Use the laptop's private IPv4 address on your Wi-Fi network.")
    return str(address)


@dataclass
class Pair:
    phone_key: str
    viewer_key: str
    expires: float
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    viewer: bool = False
    sender: bool = False
    closed: bool = False
    latest: bytes | None = None
    sequence: int = 0


class PhoneRelay:
    def __init__(self, origin=None, clock=monotonic):
        self.origin = origin
        self.clock = clock
        self.pairs = {}

    def new(self):
        if not self.origin:
            raise HTTPException(503, "Phone pairing is disabled. Start VisionAttend in phone mode.")
        self.prune()
        if len(self.pairs) >= MAX_PAIRS:
            raise HTTPException(429, "Too many active camera pairs.")
        ident = secrets.token_urlsafe(18)
        pair = Pair(secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock() + PAIR_TTL)
        self.pairs[ident] = pair
        return {
            "pair_id": ident, "viewer_key": pair.viewer_key, "expires_in": PAIR_TTL,
            "url": f"{self.origin}/phone#{ident}.{pair.phone_key}",
        }

    def end(self, ident):
        pair = self.pairs.pop(ident, None)
        if pair:
            pair.closed = True
            pair.latest = None
            pair.changed.set()

    def prune(self):
        for ident, pair in list(self.pairs.items()):
            if self.clock() >= pair.expires:
                self.end(ident)

    def claim(self, ident, key, sender):
        self.prune()
        pair = self.pairs.get(ident)
        if (
            pair is None
            or (pair.sender if sender else pair.viewer)
            or not isinstance(key, str)
            or not key.isascii()
            or not secrets.compare_digest(key, pair.phone_key if sender else pair.viewer_key)
        ):
            return None
        if sender:
            pair.sender = True
            pair.expires = self.clock() + ACTIVE_TTL
        else:
            pair.viewer = True
        pair.changed.set()
        return pair

    async def connect(self, socket: WebSocket, raw_origins, sender):
        if socket.headers.get("origin") not in raw_origins:
            await socket.close(code=1008)
            return
        if sender and socket.headers.get("host") != self.origin.removeprefix("https://"):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            text = await asyncio.wait_for(socket.receive_text(), 5)
            if len(text) > 1024:
                raise ValueError()
            message = json.loads(text)
            ident = message["pair_id"]
            pair = self.claim(ident, message["key"], sender)
        except (ValueError, KeyError, TypeError, RuntimeError, asyncio.TimeoutError, WebSocketDisconnect):
            await socket.close(code=1008)
            return
        if pair is None:
            await socket.close(code=1008, reason="Pairing is invalid or expired.")
            return
        if sender:
            await self.send_camera(socket, ident, pair)
        else:
            await self.view_camera(socket, ident, pair)

    async def send_camera(self, socket, ident, pair):
        last_frame = float("-inf")
        try:
            await socket.send_json({"status": "connected"})
            while not pair.closed:
                remaining = pair.expires - self.clock()
                if remaining <= 0:
                    break
                frame = await asyncio.wait_for(socket.receive_bytes(), min(remaining, 8))
                if pair.closed:
                    break
                if not (HEADER.size < len(frame) <= HEADER.size + MAX_FRAME):
                    await socket.close(code=1009)
                    return
                now = self.clock()
                if now - last_frame < 0.085:
                    continue
                fps, mirrored = HEADER.unpack_from(frame)
                if not math.isfinite(fps) or not 0 <= fps <= 240 or mirrored not in (0, 1):
                    await socket.close(code=1008)
                    return
                try:
                    jpeg_dimensions(frame[HEADER.size:])
                except ValueError:
                    continue
                last_frame = now
                pair.latest = frame
                pair.sequence += 1
                pair.changed.set()
            await socket.close(code=1000)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except asyncio.TimeoutError:
            await socket.close(code=1000)
        finally:
            self.end(ident)

    async def view_camera(self, socket, ident, pair):
        async def relay_frames():
            previous = 0
            connected = False
            while not pair.closed:
                pair.changed.clear()
                if not connected and pair.sender:
                    await socket.send_json({"status": "connected"})
                    connected = True
                if pair.sequence != previous and pair.latest is not None:
                    frame, previous = pair.latest, pair.sequence
                    await asyncio.wait_for(socket.send_bytes(frame), 2)
                remaining = pair.expires - self.clock()
                if remaining <= 0:
                    break
                await asyncio.wait_for(pair.changed.wait(), remaining)

        async def watch_viewer():
            while True:
                if (await socket.receive())["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(relay_frames()), asyncio.create_task(watch_viewer())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.end(ident)
            try:
                await socket.close(code=1000)
            except (WebSocketDisconnect, RuntimeError):
                pass


def serve_phone(relay: PhoneRelay, dist):
    phone = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    phone.router.routes.append(Mount("/assets", app=StaticFiles(directory=dist / "assets"), name="assets"))

    @phone.middleware("http")
    async def companion_headers(request, call_next):
        if request.headers.get("host") != relay.origin.removeprefix("https://"):
            from fastapi.responses import Response
            return Response(status_code=400)
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=()"
        return response

    @phone.get("/phone")
    async def camera():
        return FileResponse(dist / "index.html")

    @phone.websocket("/ws/camera")
    async def stream(socket: WebSocket):
        await relay.connect(socket, {relay.origin}, sender=True)

    return phone
