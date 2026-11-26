"""Start the loopback app and, when requested, a camera-only HTTPS listener."""
import argparse
import asyncio
from contextlib import nullcontext

import uvicorn

from app import DIST, create_app
from vision_models import MAX_FRAME
from vision_phone import PhoneRelay, private_address, serve_phone


class CompanionServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


async def run(port, phone_ip, phone_port, cert, key):
    origin = f"https://{phone_ip}:{phone_port}" if phone_ip else None
    relay = PhoneRelay(origin)
    desktop = uvicorn.Server(uvicorn.Config(
        create_app(phone=relay, desktop_port=port), host="127.0.0.1", port=port,
        ws_max_size=MAX_FRAME + 8, ws_max_queue=1, access_log=False,
    ))
    if not phone_ip:
        await desktop.serve()
        return

    companion = CompanionServer(uvicorn.Config(
        serve_phone(relay, DIST), host=phone_ip, port=phone_port,
        ssl_certfile=cert, ssl_keyfile=key, ws_max_size=MAX_FRAME + 8, ws_max_queue=1, access_log=False,
    ))
    tasks = [asyncio.create_task(server.serve()) for server in (desktop, companion)]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        desktop.should_exit = companion.should_exit = True
        await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, help="Loopback app port")
    parser.add_argument("--phone-ip", help="Laptop's private Wi-Fi IPv4 address")
    parser.add_argument("--phone-port", type=int, default=8443)
    parser.add_argument("--cert", help="PEM certificate trusted by the phone")
    parser.add_argument("--key", help="PEM private key for the certificate")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use an app port between 1024 and 65535.")
    if args.phone_ip:
        try:
            args.phone_ip = private_address(args.phone_ip)
        except ValueError as exc:
            parser.error(str(exc))
        if not args.cert or not args.key:
            parser.error("Phone mode requires --cert and --key.")
        if not (DIST / "assets").is_dir():
            parser.error("Build the React app before using phone mode.")
        if not 1024 <= args.phone_port <= 65535:
            parser.error("Use a phone port between 1024 and 65535.")
    elif args.cert or args.key:
        parser.error("Specify --phone-ip to enable phone mode.")
    asyncio.run(run(args.port, args.phone_ip, args.phone_port, args.cert, args.key))


if __name__ == "__main__":
    main()
