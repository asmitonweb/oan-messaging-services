"""Subprocess harness for test_int_process.py (not collected by pytest).

Runs the REAL API (``create_app()`` with its production lifespan: settings, policy and
secrets from ``OAN_MSG_*`` environment, PostgreSQL repository, credential authentication)
under uvicorn on loopback.

Harness-only wrapper: a request carrying ``X-It-Hold-Response`` is processed normally, so
its transaction has committed, but its response start is withheld forever. The test then
SIGKILLs this process: a genuinely lost response, not a simulated exception.

For callers that cannot add a header (the accepted MSG-06 client sends only contract
headers), ``IT_HOLD_POSTS=<n>`` withholds the responses of the first ``n`` POST requests
instead. Optional ``argv[2]``/``argv[3]`` (certificate, key) serve HTTPS on loopback, which
the client requires (it refuses non-https base URLs).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import MutableMapping
from typing import Any

import uvicorn

from oan_messaging.api import create_app

_connect = socket.socket.connect


def _loopback_only(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise OSError("harness refuses non-loopback connections")
    return _connect(self, address)


socket.socket.connect = _loopback_only  # type: ignore[assignment]

app = create_app()
_hold_posts = {"left": int(os.environ.get("IT_HOLD_POSTS", "0"))}


def _should_hold(scope: MutableMapping[str, Any]) -> bool:
    if scope["type"] != "http":
        return False
    if any(k == b"x-it-hold-response" for k, _ in scope["headers"]):
        return True
    if scope["method"] == "POST" and _hold_posts["left"] > 0:
        _hold_posts["left"] -= 1
        return True
    return False


async def harness(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
    if _should_hold(scope):

        async def withhold(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                print("IT-RESPONSE-WITHHELD", flush=True)
                await asyncio.Event().wait()
            await send(message)

        await app(scope, receive, withhold)
        return
    await app(scope, receive, send)


if __name__ == "__main__":
    tls: dict[str, str] = {}
    if len(sys.argv) >= 4:
        tls = {"ssl_certfile": sys.argv[2], "ssl_keyfile": sys.argv[3]}
    uvicorn.run(
        harness,
        host="127.0.0.1",
        port=int(sys.argv[1]),
        log_level="info",
        lifespan="on",
        **tls,  # type: ignore[arg-type]
    )
