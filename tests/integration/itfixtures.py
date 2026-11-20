"""MSG-07 shared fixtures (imported by tests/integration and tests/security conftests).

REAL local PostgreSQL only (``OAN_MSG_TEST_DATABASE_URL``, an admin URL on loopback). Each
session creates ``oan_msg_itest_<random>``, migrates it from EMPTY with Alembic and drops it
at the end; tables are truncated before each test. No SQLite/in-memory fallback. Python-level
outbound connections are refused except loopback (TLS ingress, loopback fake providers).
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from itsupport import (
    ALL_TABLES,
    ROOT,
    Certs,
    FakeClock,
    Harness,
    Keys,
    build_harness,
    create_database,
    drop_database,
    make_certs,
    new_keys,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from oan_messaging.storage.db import make_engine, make_session_factory


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["skip_logging_config"] = True
    return cfg


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
    url = create_database(admin_url)
    command.upgrade(alembic_config(url), "head")
    yield url
    drop_database(admin_url, url)


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


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Certs:
    return make_certs(tmp_path_factory.mktemp("it-certs"))


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
async def h(
    engine: AsyncEngine, sf: async_sessionmaker[AsyncSession], keys: Keys, clock: FakeClock
) -> AsyncIterator[Harness]:
    harness = build_harness(engine, sf, keys, clock)
    yield harness
    await harness.close()
