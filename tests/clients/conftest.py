"""Fixtures for MSG-06 client tests. MOCKED HTTP ONLY: no network, no database, fake
credentials, controlled 0900000xxx test numbers. Nothing here is a real integration.

``ApiStore`` is a tiny in-memory stand-in for the accepted repository, so that requests
can be run through the REAL accepted FastAPI app (oan_messaging.api) for contract
compatibility. It applies the contract's own idempotency/expiry rules, but it is not
storage evidence. PostgreSQL behaviour is covered by the accepted MSG-02 suite.
"""

from __future__ import annotations

import socket
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))

from oan_client import ClientConfig, OanClient, Secret  # noqa: E402

from oan_messaging.api import ApiDependencies, create_app  # noqa: E402
from oan_messaging.contracts import (  # noqa: E402
    PURPOSE_PRIORITY,
    CallerContext,
    Channel,
    FingerprintDomain,
    HmacFingerprinter,
    IdempotencyConflict,
    IdempotencyDecision,
    MessageStatus,
    Purpose,
    SmsSubmission,
    StoredMessage,
    Submission,
    canonical_payload,
    check_expiry,
    classify_idempotent_request,
    mask_chat_id,
    mask_recipient,
    submission_channel,
)
from oan_messaging.storage.errors import ExpiryRejected  # noqa: E402

T0 = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)
BASE_URL = "https://oan-messaging.test"
FAKE_TOKEN = "oanmsg_" + "F" * 43  # noqa: S105 (fake test credential)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@dataclass
class FakeClock:
    now: datetime = T0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def config(**kw: Any) -> ClientConfig:
    return ClientConfig(base_url=BASE_URL, token=Secret(FAKE_TOKEN), **kw)


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def client_for(handler: Handler, clock: FakeClock, **cfg: Any) -> tuple[OanClient, Recorder]:
    rec = Recorder(handler)
    c = OanClient(config(**cfg), transport=httpx.MockTransport(rec), clock=clock,
                  sleep=clock.sleep, jitter=lambda lo, hi: hi)
    return c, rec


# --------------------------------------------- real accepted API with an in-memory store


class ApiStore:
    """In-memory AcceptanceStore applying contract rules (mock; not storage evidence)."""

    def __init__(self) -> None:
        self.fp = HmacFingerprinter({"t1": b"\x01" * 32}, "t1")
        self.rows: dict[tuple[str, str, str], StoredMessage] = {}
        self.by_id: dict[uuid.UUID, StoredMessage] = {}

    async def create_or_get(self, caller: CallerContext, key: str, sub: Submission,
                            now: datetime) -> tuple[StoredMessage, IdempotencyDecision]:
        channel = submission_channel(sub)
        scope = (caller.caller_id, channel.value, key)
        cand = self.fp.fingerprint(FingerprintDomain.PAYLOAD,
                                   canonical_payload(caller.caller_id, sub))
        existing = self.rows.get(scope)
        decision = classify_idempotent_request(existing, cand if existing else None)
        if decision is IdempotencyDecision.CONFLICT:
            raise IdempotencyConflict("conflict")
        if decision is IdempotencyDecision.REPLAY:
            assert existing is not None
            return existing, decision
        try:
            check_expiry(sub.purpose, sub.expires_at, now)
        except ValueError as exc:
            msg = str(exc)
            raise ExpiryRejected("required_for_otp" if "required" in msg else
                                 "not_in_future" if "future" in msg and "far" not in msg
                                 else "beyond_ceiling") from None
        is_sms = isinstance(sub, SmsSubmission)
        row = StoredMessage(
            id=uuid.uuid4(), caller_id=caller.caller_id, channel=channel, idempotency_key=key,
            purpose=sub.purpose, priority=PURPOSE_PRIORITY[sub.purpose],
            source_reference=sub.source_reference, status=MessageStatus.ACCEPTED,
            payload_fingerprint=cand,
            recipient_fingerprint=(self.fp.fingerprint(FingerprintDomain.RECIPIENT,
                                                       sub.recipient.encode())
                                   if isinstance(sub, SmsSubmission) else None),
            recipient_masked=(mask_recipient(sub.recipient) if isinstance(sub, SmsSubmission)
                              else mask_chat_id(sub.chat_id)),
            attempts=0, created_at=now, updated_at=now, expires_at=sub.expires_at)
        assert is_sms or channel is Channel.TELEGRAM
        self.rows[scope] = row
        self.by_id[row.id] = row
        return row, IdempotencyDecision.NEW

    async def get_for_caller(self, caller_id: str, message_id: uuid.UUID) -> StoredMessage | None:
        row = self.by_id.get(message_id)
        return row if row is not None and row.caller_id == caller_id else None

    def set_status(self, message_id: uuid.UUID, **update: Any) -> None:
        row = self.by_id[message_id]
        new = StoredMessage.model_validate(row.model_dump() | update)
        self.by_id[message_id] = new
        self.rows[(new.caller_id, new.channel.value, new.idempotency_key)] = new


CALLER = CallerContext(caller_id="esignet", allowed_channels=frozenset(Channel),
                       allowed_purposes=frozenset(Purpose), telegram_bot="grievance")


@pytest.fixture
def api_store() -> ApiStore:
    return ApiStore()


@pytest.fixture
def api_app(api_store: ApiStore, clock: FakeClock) -> Any:
    async def auth(token: str) -> CallerContext | None:
        return CALLER if token == FAKE_TOKEN else None

    return create_app(ApiDependencies(repository=api_store, authenticate=auth, clock=clock))


@pytest.fixture
async def api_client(api_app: Any, clock: FakeClock) -> AsyncIterator[OanClient]:
    c = OanClient(config(), transport=httpx.ASGITransport(app=api_app), clock=clock,
                  sleep=clock.sleep, jitter=lambda lo, hi: hi)
    yield c
    await c.aclose()
