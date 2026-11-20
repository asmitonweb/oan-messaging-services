"""Fixtures for MSG-05 worker/receipt tests on a REAL local PostgreSQL.

``OAN_MSG_TEST_DATABASE_URL`` must be a local admin URL. Each session creates a fresh
``oan_msg_wtest_<random>`` database, migrates it with Alembic from empty and drops it at the
end; tables are truncated before each test. No SQLite/in-memory fallback.
Outbound Python sockets are blocked except loopback (used by the TLS ingress tests).
"""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import ALL_TABLES, FakeClock, Keys, new_keys

from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository

ROOT = Path(__file__).resolve().parents[2]


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("OAN_MSG_TEST_DATABASE_URL")
    if not url:
        pytest.fail("OAN_MSG_TEST_DATABASE_URL is required (real local PostgreSQL only)")
    host = url.split("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("OAN_MSG_TEST_DATABASE_URL must point to a local database")
    return url


@pytest.fixture(scope="session")
def database_url(admin_url: str) -> Iterator[str]:
    name = f"oan_msg_wtest_{secrets.token_hex(4)}"
    with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = admin_url.rsplit("/", 1)[0] + "/" + name
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")
    yield url
    with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError("outbound network connection attempted")
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def keys() -> Keys:
    return new_keys()


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


@pytest.fixture
def repo(sf: async_sessionmaker[AsyncSession], keys: Keys) -> PostgresMessageRepository:
    return keys.repo(sf)
