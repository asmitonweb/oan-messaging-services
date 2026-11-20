"""MSG-08 / review finding F-09: every runtime component works under the least-privilege role
created by ``deploy/sql/grants.sql``, and that role cannot change the schema.

A LOGIN role with a generated password is created in the local test cluster and dropped
afterwards. The migration owner in this test is the admin role that ran Alembic.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import (
    FakeClock,
    FakeSms,
    Keys,
    make_caller,
    make_certs,
    make_worker,
    sent,
    submit_sms,
)

from oan_messaging.contracts import Channel, Provider
from oan_messaging.receipts import ReceiptProcessor, load_pins
from oan_messaging.runtime.ops import ops_status, set_scope
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.schema import dispatch_controls, messages, receipts
from oan_messaging.worker.cleanup import run_cleanup

ROOT = Path(__file__).resolve().parents[2]
GRANTS = ROOT / "deploy" / "sql" / "grants.sql"


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def render_grants(runtime_role: str, owner_role: str) -> str:
    """What ``psql -v runtime_role=... -v owner_role=...`` does with the script."""
    sql = GRANTS.read_text(encoding="utf-8")
    return sql.replace(':"runtime_role"', f'"{runtime_role}"').replace(
        ':"owner_role"', f'"{owner_role}"'
    )


@pytest.fixture
def runtime_url(database_url: str, admin_url: str) -> Iterator[str]:
    role = f"oan_msg_rt_{secrets.token_hex(4)}"
    password = secrets.token_hex(16)
    owner = admin_url.split("://", 1)[1].split(":", 1)[0]
    with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
        conn.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
    with psycopg.connect(_libpq(database_url), autocommit=True) as conn:
        conn.execute(render_grants(role, owner))
    _, rest = database_url.split("://", 1)[1].split("@", 1)
    yield f"postgresql+psycopg://{role}:{password}@{rest}"
    with psycopg.connect(_libpq(database_url), autocommit=True) as conn:
        conn.execute(f'DROP OWNED BY "{role}"')
    with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
        conn.execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture
async def rt_engine(runtime_url: str, engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(runtime_url, pool_size=5, max_overflow=0)
    yield eng
    await eng.dispose()


def receipt_body(sms_id: str) -> bytes:
    return json.dumps(
        {
            "sms_id": sms_id,
            "phone_number": "0900000001",
            "status": "Delivered",
            "event_time": "2026-09-01 08:12:19",
        }
    ).encode()


async def test_every_component_runs_under_the_runtime_role(
    rt_engine: AsyncEngine, keys: Keys, clock: FakeClock, tmp_path: Path
) -> None:
    sf: async_sessionmaker[AsyncSession] = make_session_factory(rt_engine)
    repo = keys.repo(sf)
    # API path: caller provisioning and acceptance.
    ctx = await make_caller(sf, keys, "rt-svc", bot=None)
    msg = await submit_sms(repo, ctx)
    assert (await repo.get_for_caller("rt-svc", msg.id)) is not None
    # Worker path: claim, rate reservation (advisory lock), begin, finish.
    sms = FakeSms(clock, lambda req, now: sent(Provider.SMS_8028, now, "17006100"))
    assert (await make_worker(repo, rt_engine, clock, sms=sms).run_once()).outcomes == {"sent": 1}
    # Receipt ingress path.
    certs = make_certs(tmp_path)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    peer = certs["pinned_cert"].read_bytes()
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    der = x509.load_pem_x509_certificate(peer).public_bytes(Encoding.DER)
    response = await proc.handle(
        der,
        "POST",
        "/sms-status-receiver/v1/update-status",
        {"content-type": "application/json"},
        receipt_body("17006100"),
    )
    assert response.status == 200
    # Cleanup path (recovery and purge), well after the retention windows.
    clock.advance(days=60)
    stats = await run_cleanup(repo, clock())
    assert stats.complete
    # Operator commands.
    await set_scope(sf, Channel.SMS, suspended=True, now=clock())
    await set_scope(sf, Channel.SMS, suspended=False, now=clock())
    report = await ops_status(sf, clock())
    assert report["provider_scopes"]["sms"]["suspended"] is False
    async with sf() as s:
        status = (
            await s.execute(select(messages.c.status).where(messages.c.id == msg.id))
        ).scalar_one()
        assert status == "delivered"
        assert (await s.execute(select(receipts.c.disposition))).scalars().all() == ["applied"]
        assert (await s.execute(select(dispatch_controls.c.channel))).scalars().all() == ["sms"]


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE oan_probe (id int)",
        "DROP TABLE messages",
        "ALTER TABLE messages ADD COLUMN probe int",
        "TRUNCATE messages",
        "SELECT version_num FROM alembic_version",
    ],
)
async def test_runtime_role_cannot_change_schema_or_read_migration_state(
    rt_engine: AsyncEngine, statement: str
) -> None:
    async with rt_engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
            await conn.execute(text(statement))


async def test_suspend_takes_effect_before_any_send_created_the_scope_row(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    clock: FakeClock,
    engine: AsyncEngine,
) -> None:
    """Regression: suspend used UPDATE only, so it silently did nothing on a fresh
    database. It must stop dispatch immediately."""
    repo = keys.repo(sf)
    await set_scope(sf, Channel.SMS, suspended=True, now=clock())
    ctx = await make_caller(sf, keys, "fresh", bot=None)
    await submit_sms(repo, ctx)
    sms = FakeSms(clock)
    assert (await make_worker(repo, engine, clock, sms=sms).run_once()).claimed == 0
    assert sms.calls == []
    await set_scope(sf, Channel.SMS, suspended=False, now=clock())
    assert (await make_worker(repo, engine, clock, sms=sms).run_once()).outcomes == {"sent": 1}


async def test_status_reports_counts_and_alerts_without_content(
    sf: async_sessionmaker[AsyncSession], keys: Keys, clock: FakeClock
) -> None:
    repo = keys.repo(sf)
    ctx = await make_caller(sf, keys, "status", bot=None)
    await submit_sms(repo, ctx, recipient="0900000777", purpose="transactional", expires_in=None)
    fresh = await ops_status(sf, clock())
    assert fresh["messages"]["accepted"] == 1 and fresh["alerts"] == []
    clock.advance(minutes=6)  # older than the provisional 5-minute queue-age threshold
    report = await ops_status(sf, clock())
    assert report["alerts"] == ["queue_age"]
    assert report["oldest_due_accepted_seconds"] == 360.0
    assert "0900000777" not in json.dumps(report)


async def test_key_check_reports_versions_in_use_but_not_configured(
    sf: async_sessionmaker[AsyncSession], keys: Keys, clock: FakeClock
) -> None:
    import os as _os

    from oan_messaging.contracts import HmacFingerprinter
    from oan_messaging.runtime.ops import missing_key_versions
    from oan_messaging.storage.crypto import ContentCipher

    repo = keys.repo(sf)
    ctx = await make_caller(sf, keys, "keys", bot=None)
    await submit_sms(repo, ctx)
    right = await missing_key_versions(
        repo,
        ContentCipher(keys.content, keys.content_current),
        HmacFingerprinter(keys.fingerprint, keys.fingerprint_current),
        clock(),
    )
    assert right == []
    wrong = await missing_key_versions(
        repo,
        ContentCipher({"zz9": _os.urandom(32)}, "zz9"),
        HmacFingerprinter({"yy9": _os.urandom(32)}, "yy9"),
        clock(),
    )
    assert wrong == [f"content:{keys.content_current}", f"fingerprint:{keys.fingerprint_current}"]
