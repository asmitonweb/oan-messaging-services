"""MSG-07 harness for the accepted MSG-06 client (``examples/oan_client``) and the outbox
example (``examples/outbox_dispatch.py``) against the REAL accepted API and PostgreSQL.

What is real and what is not:

* The client, the outbox functions, the FastAPI application, the PostgreSQL repository,
  the worker, the rate limiter and the adapters are the accepted code, unmodified.
* The HTTP hop between client and API is an in-process loopback transport
  (``httpx.ASGITransport``) wrapped by ``FaultTransport``. ``FaultTransport`` is the only
  fake: it stands for the network and any gateway in front of the API. It can lose a
  response AFTER the application has committed, refuse a connection BEFORE the
  application is reached, or answer on the application's behalf (for example a gateway
  ``503`` with ``Retry-After``, which the accepted API itself never sends).
* ``tests/integration/test_int_client_process.py`` uses real TLS sockets on loopback and a
  real API process instead.

This is not the in-memory ``ApiStore`` used by ``tests/clients``: every request that is
forwarded reaches the PostgreSQL repository.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from itsupport import ROOT, FakeClock, Harness, needle_forms

EXAMPLES = ROOT / "examples"
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

from oan_client import ClientConfig, OanClient, Secret  # noqa: E402
from outbox_dispatch import OutboxRow  # noqa: E402

BASE_URL = "https://oan-messaging.test"

#: A fault decides what happens to one request. It receives the request and a function
#: that forwards it to the real application, and returns the response the client sees
#: (or raises an httpx transport error).
Forward = Callable[[], Awaitable[httpx.Response]]
Fault = Callable[[httpx.Request, Forward], Awaitable[httpx.Response]]


@dataclass
class Seen:
    method: str
    path: str
    key: str | None
    content: bytes
    forwarded: bool = False
    status: int | None = None


class FaultTransport(httpx.AsyncBaseTransport):
    """Loopback transport to the real application with scripted network faults.

    ``faults`` are consumed one per request, in order, by requests matching ``only``
    (default: every request). When none is left the request is forwarded untouched.
    """

    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.faults: list[Fault] = []
        self.seen: list[Seen] = []

    def count(self, method: str) -> int:
        return sum(1 for s in self.seen if s.method == method)

    def forwarded(self, method: str) -> int:
        return sum(1 for s in self.seen if s.method == method and s.forwarded)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        record = Seen(
            request.method,
            request.url.path,
            request.headers.get("idempotency-key"),
            request.content,
        )
        self.seen.append(record)

        async def forward() -> httpx.Response:
            record.forwarded = True
            response = await self._inner.handle_async_request(request)
            await response.aread()
            return response

        fault = self.faults.pop(0) if self.faults else None
        response = await (fault(request, forward) if fault else forward())
        record.status = response.status_code
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------- faults


async def lose_response(request: httpx.Request, forward: Forward) -> httpx.Response:
    """The application processes and COMMITS the request; the response never arrives."""
    await forward()
    raise httpx.ReadTimeout("response lost after commit (simulated)", request=request)


async def refuse_connection(request: httpx.Request, _forward: Forward) -> httpx.Response:
    """The request never reaches the application."""
    raise httpx.ConnectError("connection refused (simulated)", request=request)


def gateway_status(
    status: int, *, retry_after: str | None = None, body: bytes | None = None
) -> Fault:
    """A gateway answers instead of the application (nothing is stored)."""

    async def fault(_request: httpx.Request, _forward: Forward) -> httpx.Response:
        headers = {"content-type": "application/problem+json"}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        payload = (
            body
            if body is not None
            else json.dumps(
                {"title": "Service unavailable", "status": status, "code": "service_unavailable"}
            ).encode()
        )
        return httpx.Response(status, content=payload, headers=headers)

    return fault


def stored_then_gateway_status(status: int, *, retry_after: str | None = None) -> Fault:
    """The application COMMITS, then a gateway replaces its answer with an error: the
    worst case the contract describes for 5xx ("may already be stored")."""

    async def fault(request: httpx.Request, forward: Forward) -> httpx.Response:
        await forward()
        return await gateway_status(status, retry_after=retry_after)(request, forward)

    return fault


def echoing_error(status: int) -> Fault:
    """A hostile or misconfigured hop that echoes the request body, the credential and the
    Idempotency-Key inside an error body, in every place a client might read."""

    async def fault(request: httpx.Request, _forward: Forward) -> httpx.Response:
        sent = request.content.decode("utf-8", "replace")
        auth = request.headers.get("authorization", "")
        payload = {
            "title": f"bad request: {sent}",
            "status": status,
            "code": f"validation_failed {sent} {auth}",
            "detail": sent,
            "errors": [
                {"field": sent, "code": sent},
                {"field": "recipient", "code": auth},
                {"field": "text", "code": "blank", "input": sent},
            ],
        }
        return httpx.Response(
            status,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/problem+json"},
        )

    return fault


# ---------------------------------------------------------------------------- client


@dataclass
class ClientRig:
    client: OanClient
    transport: FaultTransport
    clock: FakeClock
    slept: list[float] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.client.aclose()


def make_rig(h: Harness, token: str, **cfg: Any) -> ClientRig:
    """The accepted client wired to the real application of ``h``. Client and service
    share the harness's simulated clock; client sleeps advance it and are recorded."""
    transport = FaultTransport(h.app)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await h.clock.sleep(seconds)

    client = OanClient(
        ClientConfig(base_url=BASE_URL, token=Secret(token), **cfg),
        transport=transport,
        clock=h.clock,
        sleep=sleep,
        jitter=lambda _lo, hi: hi,  # deterministic: always the longest allowed wait
    )
    return ClientRig(client, transport, h.clock, slept)


# ---------------------------------------------------------------------------- outbox


def persist_and_reload(row: OutboxRow) -> OutboxRow:
    """What an owning service does between sweeps or across a restart: write the row to
    its own table and read it back. JSON text stands for the stored record."""
    stored = json.dumps(row.to_record(), sort_keys=True)
    return OutboxRow.from_record(json.loads(stored))


# ----------------------------------------------------------------------- log capture


class LogCapture(logging.Handler):
    """Root handler at DEBUG that records fully formatted records, including exception
    text, from every logger (``oan_client``, ``httpx``, ``httpcore``, service loggers)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def sensitive_needles(
    *, texts: list[str], numbers: list[str], tokens: list[str], extra: list[str] | None = None
) -> list[str]:
    out = [*texts, *tokens, *(extra or [])]
    for n in numbers:
        out += needle_forms(n)
    return [n for n in out if n]


def assert_clean(text: str, needles: list[str], where: str) -> None:
    # Report only the position in the needle list, never the needle itself.
    for i, needle in enumerate(needles):
        assert needle not in text, f"{where} contains sensitive value #{i}"


def exception_surface(exc: BaseException) -> str:
    """Everything an owning service could log or store from a client exception."""
    import traceback

    parts = [str(exc), repr(exc), repr(exc.args), repr(vars(exc))]
    parts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return "\n".join(parts)


def at(clock: FakeClock) -> datetime:
    return clock()
