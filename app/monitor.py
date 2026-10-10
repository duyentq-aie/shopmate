"""Local monitor: a read-only view of the request traces. Loopback only, no authentication, nothing else on disk.

    uv run python -m app.monitor            # http://127.0.0.1:8765/monitor/

Two files are ever read, both fixed in code and never chosen by a request:
  GET /monitor/                   dashboard/index.html
  GET /monitor/api/traces?limit=N the last N complete lines of logs/traces.jsonl (default 5000, 1..20000, else 422) as
                                  text/plain; a missing or empty file is 200 with an empty body
  GET /monitor                    307 to monitor/, because the page fetches the RELATIVE path `api/traces` and a page at
                                  /monitor (no slash) would resolve it to /api/traces
There are no other routes: no path parameters, no static mount, no directory listing, no /docs or /openapi.json.

Reachable from this machine only, in two layers:
  1. the launcher refuses to start on any host that is not a loopback address;
  2. a guard in front of every route answers 403 to any request whose peer address is not loopback, so even
     `uvicorn app.monitor:app --host 0.0.0.0` exposes nothing. The peer address is the socket's; forwarding headers are
     never read (the launcher also turns uvicorn's proxy-header handling off).
There is no token mode. Remote access later belongs behind a reverse proxy with real authentication, not in this app
(docs/DECISIONS-monitor.md).

The trace file is read from the end in blocks, so memory follows the number of lines asked for and not the size of the
file; the response is cut to whole lines within MAX_RESPONSE_BYTES, and a last line still being written is left out.
The app only reads: files are opened "rb", and nothing in the pipeline imports this module.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse, Response

from app.config import DEFAULT_ENV_FILE, REPO_ROOT, get_setting
from app.tracing import TRACE_PATH

DASHBOARD_PAGE = REPO_ROOT / "dashboard" / "index.html"
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765
DEFAULT_LIMIT, MAX_LIMIT = 5000, 20_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
BLOCK_BYTES = 64 * 1024

# What the page actually loads: its own inline script and style, Google Fonts, and fetch() to its own origin.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
SECURITY_HEADERS = ((b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"content-security-policy", CSP.encode("ascii")))


# --- who may connect ----------------------------------------------------------------------------------------------------------


def is_loopback_ip(host) -> bool:
    """True only for an IP literal in 127.0.0.0/8 or ::1 (also as an IPv4-mapped IPv6 address). Names, blanks, odd
    spellings of numbers and anything unparseable are not loopback."""
    if not isinstance(host, str):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def is_loopback_host(host) -> bool:
    """A host the server may bind to: a loopback IP literal, or the name `localhost`."""
    return host == "localhost" or is_loopback_ip(host)


class _LoopbackOnly:
    """ASGI guard in front of every route: 403 unless the socket's peer is loopback; adds the security headers to every
    response. Fails closed (no peer address, or one that is not an IP, is refused). Reads no header."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):  # lifespan
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if not client or not is_loopback_ip(client[0]):
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", b"10"), *SECURITY_HEADERS]})
                await send({"type": "http.response.body", "body": b"Forbidden\n"})
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *SECURITY_HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --- reading the trace file -----------------------------------------------------------------------------------------------


def tail_lines(path: Path, limit: int, *, max_bytes: int = MAX_RESPONSE_BYTES, block: int = BLOCK_BYTES) -> bytes:
    """The last `limit` complete lines of the file (each with its newline), oldest first; b"" if it is missing or has none.

    Reads backwards in blocks until `limit` lines are certain, the start of the file, or max_bytes + one block. Only
    complete lines count: a last line without its newline (a writer is in the middle of it) is left out, and so is the
    oldest line read when a block boundary may have cut it. The size is taken once, so lines appended while reading
    are not part of the answer. If the lines are more than max_bytes, the oldest whole lines are dropped until they fit."""
    try:
        handle = open(path, "rb")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return b""
    with handle:
        position = os.fstat(handle.fileno()).st_size
        chunks: list[bytes] = []
        newlines = held = 0
        while position > 0 and newlines <= limit and held < max_bytes + block:  # limit + 1 newlines: the oldest line may be cut
            start = max(0, position - block)
            handle.seek(start)
            chunk = handle.read(position - start)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
            held += len(chunk)
            position = start
    data = b"".join(reversed(chunks))
    del chunks  # at most two copies of the lines are alive at once: the joined data, then the answer cut out of it
    end = data.rfind(b"\n") + 1  # a last line that has no newline yet is a line being written: leave it out
    first = 0 if position == 0 else data.find(b"\n", 0, end) + 1  # the oldest line may be cut by a block, unless we read from the start
    begin = end
    for _ in range(limit):
        previous = data.rfind(b"\n", first, begin - 1)  # the newline that ends the line before this one
        if previous == -1:
            begin = first
            break
        begin = previous + 1
    if end - begin > max_bytes:  # too much: drop the oldest whole lines
        begin = end - max_bytes
        if data[begin - 1:begin] != b"\n":  # inside a line: start after its end (nothing at all if one line is bigger than the cap)
            begin = data.find(b"\n", begin, end) + 1
    return data if (begin, end) == (0, len(data)) else data[begin:end]


# --- the app ------------------------------------------------------------------------------------------------------------------


def create_app(*, traces_path: Path = TRACE_PATH, page_path: Path = DASHBOARD_PAGE):
    """The ASGI app. The two paths are fixed when it is built; no request can change which files are read."""
    api = FastAPI(title="ShopMate monitor", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @api.get("/monitor")
    def to_the_page() -> Response:
        return RedirectResponse("monitor/", status_code=307)  # relative: /monitor -> /monitor/ (and behind a prefix, too)

    @api.get("/monitor/")
    def page() -> Response:
        try:
            with open(page_path, "rb") as handle:
                body = handle.read()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return Response(b"The dashboard page is not installed.\n", status_code=404, media_type="text/plain; charset=utf-8")
        return Response(body, media_type="text/html; charset=utf-8")

    @api.get("/monitor/api/traces")
    def traces(limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT) -> Response:
        return Response(tail_lines(traces_path, limit), media_type="text/plain; charset=utf-8")

    return _LoopbackOnly(api)


_DEFAULT_APP = app = create_app()


# --- starting it --------------------------------------------------------------------------------------------------------------


def parse_bind(argv: list[str] | None = None, *, env_file: Path = DEFAULT_ENV_FILE) -> tuple[str, int]:
    """(host, port) from --host/--port, then MONITOR_HOST/MONITOR_PORT (environment, then .env), then the defaults.
    Exits with a message, before anything is opened, if the host is not loopback."""
    parser = argparse.ArgumentParser(prog="python -m app.monitor", description="ShopMate trace monitor (loopback only).")
    parser.add_argument("--host", default=None, help=f"loopback address to bind (default {DEFAULT_HOST})")
    parser.add_argument("--port", default=None, help=f"port (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    host = args.host if args.host is not None else get_setting("MONITOR_HOST", DEFAULT_HOST, env_file)
    raw_port = args.port if args.port is not None else get_setting("MONITOR_PORT", str(DEFAULT_PORT), env_file)
    if not is_loopback_host(host):
        print(f"refusing to start: host {host!r} is not a loopback address. The monitor serves traces without authentication, so "
              "it binds to 127.0.0.1 only; for remote access put a reverse proxy with real authentication in front of it "
              "(docs/DECISIONS-monitor.md).", file=sys.stderr)
        raise SystemExit(1)
    if not (raw_port.isascii() and raw_port.isdigit() and 1 <= int(raw_port) <= 65535):
        parser.error(f"--port must be a whole number from 1 to 65535, got {raw_port!r}")
    return host, int(raw_port)


def make_config(host: str, port: int, app=None) -> uvicorn.Config:
    """uvicorn's configuration. Proxy headers are off: the peer address the guard checks is the socket's own."""
    return uvicorn.Config(app if app is not None else _DEFAULT_APP, host=host, port=port, proxy_headers=False, log_level="info")


def main(argv: list[str] | None = None) -> int:
    host, port = parse_bind(argv)
    print(f"ShopMate monitor: http://{host}:{port}/monitor/  (read-only, loopback only; Ctrl+C to stop)", file=sys.stderr)
    uvicorn.Server(make_config(host, port)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
