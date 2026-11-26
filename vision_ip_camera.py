"""Open a local IP camera in an isolated worker and relay its latest JPEG previews."""
import asyncio
import ipaddress
import json
import struct
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from vision_models import MAX_FRAME
from vision_phone import HEADER, private_address

LENGTH = struct.Struct(">I")
WORKER = Path(__file__).resolve().parent / "ip_camera_worker.py"


class IPCameras:
    def __init__(self):
        self.active = 0

    async def serve(self, socket: WebSocket):
        if self.active >= 4:
            await socket.close(code=1013)
            return
        self.active += 1
        child = None
        tasks = []
        try:
            await socket.accept()
            hello = await asyncio.wait_for(socket.receive_text(), 10)
            if len(hello) > 4096:
                raise ValueError("Camera URL is too long.")
            source = camera_url(json.loads(hello)["url"])
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(WORKER), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            child.stdin.write(source.encode("utf-8"))
            await child.stdin.drain()
            child.stdin.close()

            async def stream():
                while True:
                    size = LENGTH.unpack(await asyncio.wait_for(child.stdout.readexactly(LENGTH.size), 10))[0]
                    if not (HEADER.size < size <= MAX_FRAME + HEADER.size):
                        raise ValueError("The camera sent an invalid frame.")
                    frame = await asyncio.wait_for(child.stdout.readexactly(size), 4)
                    await asyncio.wait_for(socket.send_bytes(frame), 2)

            async def watch():
                while (await socket.receive())["type"] != "websocket.disconnect":
                    pass

            tasks = [asyncio.create_task(stream()), asyncio.create_task(watch())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (KeyError, TypeError, ValueError):
            await safe_close(socket, "Enter an RTSP or HTTP MJPEG URL on your private network.")
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            await safe_close(socket, "Could not read the IP camera stream. Check its URL, credentials, and network.")
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if child is not None:
                if child.returncode is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                await child.wait()
            self.active -= 1


async def safe_close(socket, detail):
    try:
        await socket.send_json({"status": "error", "detail": detail})
        await socket.close(code=1000)
    except (WebSocketDisconnect, RuntimeError):
        pass


def camera_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) < 33 for char in value):
        raise ValueError("Invalid camera URL.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"rtsp", "http", "https"} or parsed.fragment or not parsed.hostname:
        raise ValueError("Unsupported camera URL.")
    private_address(str(ipaddress.IPv4Address(parsed.hostname)))
    if parsed.port == 0:
        raise ValueError("Invalid port.")
    return value
