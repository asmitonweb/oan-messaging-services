"""Fixtures for MSG-02 tests against a REAL local PostgreSQL.

Set ``OAN_MSG_TEST_DATABASE_URL`` to an admin URL (``postgresql+psycopg://.../postgres``).
Each test session creates a fresh database ``oan_msg_test_<random>``, migrates it from
empty with Alembic, and drops it at the end. Tables are truncated before every test.
There is no SQLite or in-memory fallback: without the variable the tests error out.

All keys and credentials are generated per session; no real numbers or providers.
Recipient numbers use the 0900000xxx range and are never sent anywhere.
"""

from __future__ import annotations

import base64
import os
import secrets
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from oan_messaging.api import ApiDependencies, create_app
from oan_messaging.contracts import DEFAULT_POLICY, Channel, HmacFingerprinter, Purpose
from oan_messaging.storage.credentials import authenticate, issue_credential, upsert_caller
from oan_messaging.storage.crypto import ContentCipher
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)
ALL_TABLES = (
    "dispatch_controls, receipts, message_attempts, message_content, messages, api_credentials, "
    "callers, retired_key_versions, stats_daily, stats_failures, stats_campaigns, stats_state"
)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging_config"] = True
    return cfg


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("OAN_MSG_TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "OAN_MSG_TEST_DATABASE_URL is required: MSG-02 tests run only against a "
            "real local PostgreSQL (no SQLite/in-memory substitute)."
        )
    host = url.split("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("OAN_MSG_TEST_DATABASE_URL must point to a local database")
    return url


def create_database(admin: str) -> str:
    name = f"oan_msg_test_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return admin.rsplit("/", 1)[0] + "/" + name


def drop_database(admin: str, url: str) -> None:
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(_libpq(admin), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def database_url(admin_url: str) -> Iterator[str]:
    url = create_database(admin_url)
    command.upgrade(alembic_config(url), "head")
    yield url
    drop_database(admin_url, url)


@pytest.fixture(autouse=True)
def _no_outbound_python_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Request handlers must never reach a provider. psycopg connects through libpq (C),
    so blocking Python-level socket connects does not affect the database."""

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("unexpected outbound network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@dataclass
class FakeClock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@dataclass
class Secrets:
    cipher: ContentCipher
    fingerprinter: HmacFingerprinter
    pepper: bytes
    content_keys: dict[str, bytes] = field(default_factory=dict)
    fingerprint_keys: dict[str, bytes] = field(default_factory=dict)


def new_secrets() -> Secrets:
    ck = {"c1": secrets.token_bytes(32)}
    fk = {"f1": secrets.token_bytes(32)}
    return Secrets(
        ContentCipher(ck, "c1"), HmacFingerprinter(fk, "f1"), secrets.token_bytes(32), ck, fk
    )


@pytest.fixture
def keys() -> Secrets:
    return new_secrets()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(database_url, pool_size=30, max_overflow=10)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sf(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(engine)


def make_repo(
    sf: async_sessionmaker[AsyncSession], keys: Secrets, policy: Any = DEFAULT_POLICY
) -> PostgresMessageRepository:
    return PostgresMessageRepository(
        sf, cipher=keys.cipher, fingerprinter=keys.fingerprinter, policy=policy
    )


@pytest.fixture
def repo(sf: async_sessionmaker[AsyncSession], keys: Secrets) -> PostgresMessageRepository:
    return make_repo(sf, keys)


ALL_PURPOSES = frozenset(Purpose)


async def make_caller(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    caller_id: str,
    *,
    channels: frozenset[Channel] = frozenset(Channel),
    purposes: frozenset[Purpose] = ALL_PURPOSES,
    bot: str | None = "grievance",
    enabled: bool = True,
) -> str:
    await upsert_caller(
        sf, caller_id, channels=channels, purposes=purposes, telegram_bot=bot, enabled=enabled
    )
    return await issue_credential(sf, keys.pepper, caller_id, T0)


def make_client(
    repo: Any, sf: async_sessionmaker[AsyncSession], keys: Secrets, clock: FakeClock, **kw: Any
) -> httpx.AsyncClient:
    async def auth(token: str) -> Any:
        return await authenticate(sf, keys.pepper, token)

    app = create_app(ApiDependencies(repository=repo, authenticate=auth, clock=clock, **kw))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


@pytest.fixture
async def client(
    repo: PostgresMessageRepository,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(repo, sf, keys, clock) as c:
        yield c


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def sms_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "recipient": "0900000001",
        "text": "Your OAN code is 482913.",
        "source_reference": "esignet:otp:evt-1",
        "purpose": "otp",
        "expires_at": (T0 + timedelta(minutes=5)).isoformat(),
    }
    body.update(kw)
    return {k: v for k, v in body.items() if v is not None}


def tg_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "chat_id": 555000111,
        "text": "Grievance G-1 updated.",
        "source_reference": "grievance:G-1:u1",
        "purpose": "transactional",
    }
    body.update(kw)
    return {k: v for k, v in body.items() if v is not None}


def headers(token: str, key: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


def new_key() -> str:
    import uuid

    return str(uuid.uuid4())
