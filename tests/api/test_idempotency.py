"""Idempotency, concurrency, replay, retention, rollback and restart (real PostgreSQL).

Scenario ids refer to docs/architecture.md "Mandatory downstream acceptance scenarios".
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import (
    T0,
    FakeClock,
    Secrets,
    headers,
    make_caller,
    make_client,
    make_repo,
    new_key,
    sms_body,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import (
    DEFAULT_POLICY,
    FailureCode,
    HmacFingerprinter,
    Provider,
    ProviderOutcome,
    ProviderResult,
)
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.errors import BindingIntegrityError, KeyRetirementRefused
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_content, messages

SMS = "/api/v1/messages/sms"


async def rows(sf: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with sf() as s:
        return [dict(r) for r in (await s.execute(select(messages))).mappings().all()]


def result(outcome: ProviderOutcome, at: Any, **kw: Any) -> ProviderResult:
    base: dict[str, Any] = {
        "outcome": outcome,
        "provider": Provider.SMS_8028,
        "provider_status": "http_200",
        "started_at": at,
        "finished_at": at,
    }
    if outcome is ProviderOutcome.SENT:
        base["provider_message_id"] = kw.pop("pid", "17006100")
    base.update(kw)
    return ProviderResult(**base)


async def drive(
    repo: PostgresMessageRepository, now: Any, outcome: ProviderOutcome | None, **kw: Any
) -> None:
    """Claim the next message, begin an attempt and (optionally) finish it."""
    (claim,) = await repo.claim_due("w1", now, timedelta(minutes=2), 1)
    await repo.begin_attempt(claim, now)
    if outcome is not None:
        await repo.finish_attempt(claim, result(outcome, now, **kw), None)


# --------------------------------------------------------------- A1 concurrency


async def test_a1_concurrent_identical_requests_one_row(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="oan_messaging.storage.repository")
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    responses = await asyncio.gather(
        *[client.post(SMS, json=sms_body(), headers=headers(token, key)) for _ in range(25)]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 24 + [202], codes
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(await rows(sf)) == 1
    # The race was real: some requests passed the lookup, hit the unique index and
    # resolved through ON CONFLICT against the committed winner.
    races = [r for r in caplog.records if "lost a race" in r.getMessage()]
    assert races, "no concurrent insert race was exercised"
    async with sf() as s:
        assert (
            await s.execute(select(func.count()).select_from(message_content))
        ).scalar_one() == 1


async def test_a1_concurrent_conflicting_requests_one_row(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    bodies = [sms_body(text=f"Your OAN code is 48291{i}.") for i in range(10)]
    responses = await asyncio.gather(
        *[client.post(SMS, json=b, headers=headers(token, key)) for b in bodies]
    )
    codes = [r.status_code for r in responses]
    assert codes.count(202) == 1 and set(codes) <= {202, 409}
    assert len(await rows(sf)) == 1


# ----------------------------------------------------------- replay semantics


async def test_a3_a4_a5_replay_after_purge_and_expiry_and_conflict(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    async with make_client(repo, sf, keys, clock) as c:
        first = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert first.status_code == 202
        await drive(repo, T0 + timedelta(seconds=1), ProviderOutcome.SENT)
        (row,) = await rows(sf)
        assert row["status"] == "sent" and row["content_purged_at"] is not None  # A3 purge
        clock.advance(hours=2)  # far past the OTP's expires_at (A4)
        replay = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
        assert replay.json()["status"] == "sent"
        # Same body with an equivalent phone format and equivalent instant: still a replay.
        same = sms_body(
            recipient="+251900000001",
            expires_at=(T0 + timedelta(minutes=5))
            .astimezone(__import__("datetime").timezone(timedelta(hours=3)))
            .isoformat(),
        )
        assert (await c.post(SMS, json=same, headers=headers(token, key))).status_code == 200
        # A5: changed content after purge -> 409 (fingerprint, not content, decides).
        changed = await c.post(
            SMS, json=sms_body(text="Your OAN code is 999999."), headers=headers(token, key)
        )
        assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
        assert "999999" not in changed.text
        other_recipient = await c.post(
            SMS, json=sms_body(recipient="0900000002"), headers=headers(token, key)
        )
        assert other_recipient.status_code == 409
    assert len(await rows(sf)) == 1


async def test_a7_commit_then_lost_response_then_same_key_retry(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()

    class LosesResponse:
        """Commits through the real repository, then fails before the response."""

        def __init__(self) -> None:
            self.fail = True

        async def create_or_get(self, *a: Any) -> Any:
            out = await repo.create_or_get(*a)
            if self.fail:
                self.fail = False
                raise ConnectionResetError("connection dropped after commit")
            return out

        async def get_for_caller(self, *a: Any) -> Any:
            return await repo.get_for_caller(*a)

    async with make_client(LosesResponse(), sf, keys, clock) as c:
        lost = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert lost.status_code == 503
        assert lost.json()["errors"] == [{"code": "outcome_uncertain_retry_same_key"}]
        (row,) = await rows(sf)  # it WAS stored: 503 does not mean "not stored"
        retry = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert retry.status_code == 200 and retry.json()["id"] == str(row["id"])
        # A8: after an uncertain response, a changed body is a conflict, not a new message.
        changed = await c.post(
            SMS, json=sms_body(text="Different 111111."), headers=headers(token, key)
        )
        assert changed.status_code == 409
    assert len(await rows(sf)) == 1


async def test_a9_replay_across_fingerprint_key_rotation_and_a9b_retirement(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    old_repo = make_repo(sf, keys)
    async with make_client(old_repo, sf, keys, clock) as c:
        first = await c.post(
            SMS, json=sms_body(purpose="alert", expires_at=None), headers=headers(token, key)
        )
    # Rotate: f2 becomes current, f1 kept for verification.
    rotated = Secrets(
        keys.cipher,
        HmacFingerprinter({**keys.fingerprint_keys, "f2": b"\x07" * 32}, "f2"),
        keys.pepper,
    )
    new_repo = make_repo(sf, rotated)
    async with make_client(new_repo, sf, keys, clock) as c:
        replay = await c.post(
            SMS, json=sms_body(purpose="alert", expires_at=None), headers=headers(token, key)
        )
        assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
        conflict = await c.post(
            SMS,
            json=sms_body(purpose="alert", expires_at=None, text="changed"),
            headers=headers(token, key),
        )
        assert conflict.status_code == 409
        second = await c.post(
            SMS, json=sms_body(purpose="alert", expires_at=None), headers=headers(token, new_key())
        )
        assert second.status_code == 202
    versions = {r["id"]: r["payload_fp_version"] for r in await rows(sf)}
    assert versions[uuid.UUID(first.json()["id"])] == "f1"
    assert versions[uuid.UUID(second.json()["id"])] == "f2"
    assert await new_repo.fingerprint_key_versions_in_use(T0) == frozenset({"f1", "f2"})
    with pytest.raises(KeyRetirementRefused):
        await new_repo.retire_key_version("fingerprint", "f1", T0)  # live binding uses it
    with pytest.raises(KeyRetirementRefused):
        await new_repo.retire_key_version("fingerprint", "f2", T0)  # current


async def test_retired_key_cannot_be_used_concurrently(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    """Retirement is recorded under an exclusive lock; afterwards writers refuse it."""
    token = await make_caller(sf, keys, "esignet")
    rotated = Secrets(
        keys.cipher,
        HmacFingerprinter({**keys.fingerprint_keys, "f2": b"\x07" * 32}, "f2"),
        keys.pepper,
    )
    repo_new = make_repo(sf, rotated)
    await repo_new.retire_key_version("fingerprint", "f1", T0)  # nothing references f1
    # A stale process still configured with f1 as current must not write with it.
    async with make_client(make_repo(sf, keys), sf, keys, clock) as c:
        r = await c.post(SMS, json=sms_body(), headers=headers(token, new_key()))
        assert r.status_code == 503
    assert await rows(sf) == []


# --------------------------------------------------------- binding retention


async def test_a17_outstanding_bindings_kept_beyond_30_days(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "advisory")
    k_acc, k_send, k_unk = new_key(), new_key(), new_key()
    b = {
        k: sms_body(purpose="broadcast", expires_at=None, recipient=f"090000000{i}")
        for i, k in enumerate((k_acc, k_send, k_unk), start=1)
    }
    async with make_client(repo, sf, keys, clock) as c:
        for k in (k_unk, k_send, k_acc):  # claim order = created order
            assert (await c.post(SMS, json=b[k], headers=headers(token, k))).status_code == 202
            clock.advance(seconds=1)
        await drive(
            repo,
            clock.now,
            ProviderOutcome.UNKNOWN,
            failure_code=FailureCode.TIMEOUT,
            provider_status="timeout",
        )
        await drive(repo, clock.now, None)  # left in sending (lease 2 min)
        late = T0 + timedelta(days=31)
        clock.now = late
        await repo.purge_due(late, 1000)
        statuses = {r["idempotency_key"]: r for r in await rows(sf)}
        assert statuses[k_unk]["status"] == "unknown"
        assert statuses[k_send]["status"] == "sending"
        assert statuses[k_acc]["status"] == "accepted"
        assert all(
            r["binding_released_at"] is None and r["payload_fp_digest"] for r in statuses.values()
        )
        for k in (k_acc, k_send, k_unk):
            again = await c.post(SMS, json=b[k], headers=headers(token, k))
            assert again.status_code == 200 and again.json()["id"] == str(statuses[k]["id"])
            changed = await c.post(SMS, json={**b[k], "text": "changed"}, headers=headers(token, k))
            assert changed.status_code == 409
    assert len(await rows(sf)) == 3


async def test_a6_completed_binding_released_after_window_status_lookup_kept(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "advisory")
    key = new_key()
    body = sms_body(purpose="broadcast", expires_at=None)
    async with make_client(repo, sf, keys, clock) as c:
        first = await c.post(SMS, json=body, headers=headers(token, key))
        await drive(repo, T0 + timedelta(seconds=1), ProviderOutcome.SENT)
        # Inside the window nothing is released.
        await repo.purge_due(T0 + DEFAULT_POLICY.dedup_window - timedelta(seconds=1), 1000)
        (row,) = await rows(sf)
        assert row["binding_released_at"] is None
        at = T0 + DEFAULT_POLICY.dedup_window
        await repo.purge_due(at, 1000)
        (row,) = await rows(sf)
        assert row["binding_released_at"] == at and row["payload_fp_digest"] is None
        clock.now = at + timedelta(seconds=1)
        view = await c.get(f"/api/v1/messages/{first.json()['id']}", headers=headers(token))
        assert view.status_code == 200 and view.json()["status"] == "sent"
        assert view.json()["idempotency_key"] == key
        # The key is free again: a resubmission is a NEW message (documented hazard).
        again = await c.post(SMS, json=body, headers=headers(token, key))
        assert again.status_code == 202 and again.json()["id"] != first.json()["id"]
    assert len(await rows(sf)) == 2


async def test_a18_live_binding_without_fingerprint_is_integrity_error(
    engine: Any,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    caller = await __import__(
        "oan_messaging.storage.credentials", fromlist=["authenticate"]
    ).authenticate(sf, keys.pepper, token)
    from oan_messaging.contracts import SmsSubmission

    sub = SmsSubmission.model_validate(sms_body())
    key = new_key()
    await repo.create_or_get(caller, key, sub, T0)
    # 1. The database refuses to create such a row at all.
    async with engine.connect() as conn:
        with pytest.raises(Exception) as info:
            await conn.execute(
                text("UPDATE messages SET payload_fp_version = NULL, payload_fp_digest = NULL")
            )
        assert "ck_messages_live_binding_fingerprint" in str(info.value)
    # 2. If one existed anyway (constraint dropped inside a rolled-back transaction),
    #    the repository raises instead of treating the request as new.
    async with engine.connect() as conn:
        trans = await conn.begin()
        await conn.execute(
            text("ALTER TABLE messages DROP CONSTRAINT ck_messages_live_binding_fingerprint")
        )
        await conn.execute(
            text("UPDATE messages SET payload_fp_version = NULL, payload_fp_digest = NULL")
        )
        joined = async_sessionmaker(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        corrupt_repo = make_repo(joined, keys)
        with pytest.raises(BindingIntegrityError):
            await corrupt_repo.create_or_get(caller, key, sub, T0)
        n = (await conn.execute(text("SELECT count(*) FROM messages"))).scalar_one()
        assert n == 1  # no second message was created
        await trans.rollback()
    (row,) = await rows(sf)
    assert row["payload_fp_digest"] is not None  # rollback restored constraint and data


# --------------------------------------------------------- rollback and restart


async def test_rollback_when_content_write_fails_leaves_nothing(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()

    class BrokenCipher:
        current_version = keys.cipher.current_version

        def encrypt(self, *a: Any) -> Any:
            raise RuntimeError("HSM unavailable")

    broken = PostgresMessageRepository(
        sf,
        cipher=BrokenCipher(),  # type: ignore[arg-type]
        fingerprinter=keys.fingerprinter,
        policy=DEFAULT_POLICY,
    )
    async with make_client(broken, sf, keys, clock) as c:
        r = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert r.status_code == 503
    assert await rows(sf) == []  # message row rolled back with the failed content write
    async with make_client(make_repo(sf, keys), sf, keys, clock) as c:
        retry = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert retry.status_code == 202


async def test_persistence_across_service_restart(
    database_url: str,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    eng1 = make_engine(database_url)
    sf1 = make_session_factory(eng1)
    async with make_client(make_repo(sf1, keys), sf1, keys, clock) as c:
        first = await c.post(SMS, json=sms_body(), headers=headers(token, key))
        assert first.status_code == 202
    await eng1.dispose()
    # Kill every other server session too: nothing may survive only in a connection.
    async with sf() as s, s.begin():
        await s.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
    eng2 = make_engine(database_url)
    sf2 = make_session_factory(eng2)
    try:
        async with make_client(make_repo(sf2, keys), sf2, keys, clock) as c:
            got = await c.get(f"/api/v1/messages/{first.json()['id']}", headers=headers(token))
            assert got.status_code == 200 and got.json()["status"] == "accepted"
            replay = await c.post(SMS, json=sms_body(), headers=headers(token, key))
            assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    finally:
        await eng2.dispose()


async def test_database_unavailable_is_uncertain_503(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    dead = make_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    dead_sf = make_session_factory(dead)
    async with make_client(make_repo(dead_sf, keys), sf, keys, clock) as c:
        r = await c.post(SMS, json=sms_body(), headers=headers(token, new_key()))
    await dead.dispose()
    assert r.status_code == 503
    assert r.json()["errors"] == [{"code": "outcome_uncertain_retry_same_key"}]
