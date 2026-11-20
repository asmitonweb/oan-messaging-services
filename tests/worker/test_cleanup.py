"""Retention cleanup driven by the worker scheduler: crash-safe content purge, outage
resume, outstanding-binding protection, completed-binding release, receipt deadlines
without sent_at and key retirement. Real PostgreSQL; fake clock."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import (
    T0,
    FakeClock,
    FakeSms,
    Keys,
    make_caller,
    make_worker,
    outcome,
    submit_broadcast,
)

from oan_messaging.contracts import DEFAULT_POLICY, FailureCode, Provider, ProviderOutcome
from oan_messaging.storage.errors import KeyRetirementRefused
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_content, messages
from oan_messaging.worker import cleanup_forever, run_cleanup

LEASE = timedelta(minutes=2)


async def row(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> dict[str, Any]:
    async with sf() as s:
        return dict(
            (await s.execute(select(messages).where(messages.c.id == mid))).mappings().one()
        )


async def content_count(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as s:
        return len((await s.execute(select(message_content.c.message_id))).all())


async def test_crash_leftover_content_purged_in_batches_after_outage(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await make_caller(sf, keys)
    ids = [(await submit_broadcast(repo, ctx, i)).id for i in range(7)]
    # Simulate crashes after the status commit but before the inline purge.
    async with sf() as s, s.begin():
        await s.execute(
            update(messages)
            .where(messages.c.id.in_(ids))
            .values(status="sending", lease_token=uuid.uuid4(), attempts=1, last_attempt_at=T0)
        )
        await s.execute(
            update(messages)
            .where(messages.c.id.in_(ids))
            .values(status="sent", sent_at=T0, provider_message_id="x")
        )
    assert await content_count(sf) == 7
    later = T0 + timedelta(hours=6)  # cleanup was down for hours
    stats = await run_cleanup(repo, later, batch=3)
    assert stats.complete and stats.batches >= 3 and await content_count(sf) == 0
    stamps = [(await row(sf, i))["content_purged_at"] for i in ids]
    assert stamps == [later] * len(ids)
    again = await run_cleanup(repo, later, batch=3)
    assert again.touched == 0 and again.complete  # idempotent


async def test_cleanup_never_releases_outstanding_bindings(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    unk = await submit_broadcast(repo, ctx, 1)
    snd = await submit_broadcast(repo, ctx, 2)
    acc = await submit_broadcast(repo, ctx, 3)
    claims = {c.message.id: c for c in await repo.claim_due("w", T0, LEASE, 3)}
    await repo.begin_attempt(claims[unk.id], T0)
    await repo.finish_attempt(
        claims[unk.id],
        outcome(Provider.SMS_8028, T0, ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT, "timeout"),
        None,
    )
    await repo.begin_attempt(claims[snd.id], T0)  # left in sending
    async with sf() as s, s.begin():  # acc: release its abandoned claim, stays accepted
        await s.execute(
            update(messages)
            .where(messages.c.id == acc.id)
            .values(lease_token=None, lease_owner=None, lease_expires_at=None)
        )
    late = T0 + DEFAULT_POLICY.dedup_window + timedelta(days=5)
    await run_cleanup(repo, late)  # recovers 'sending' -> unknown first
    for m in (unk, snd, acc):
        r = await row(sf, m.id)
        assert r["binding_released_at"] is None and r["payload_fp_digest"] is not None
    assert (await row(sf, snd.id))["status"] == "unknown"
    assert (await row(sf, acc.id))["status"] == "accepted"


async def test_completed_binding_released_after_window_and_status_lookup_kept(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    await make_worker(repo, engine, clock, sms=FakeSms(clock)).run_once()
    await run_cleanup(repo, T0 + DEFAULT_POLICY.dedup_window - timedelta(seconds=1))
    assert (await row(sf, m.id))["binding_released_at"] is None
    at = T0 + DEFAULT_POLICY.dedup_window
    await run_cleanup(repo, at)
    r = await row(sf, m.id)
    assert r["binding_released_at"] == at and r["payload_fp_digest"] is None
    looked_up = await repo.get_for_caller(ctx.caller_id, m.id)
    assert looked_up is not None and looked_up.status.value == "sent"


async def test_receipt_deadline_without_sent_at_and_no_resend(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    sms = FakeSms(
        clock,
        lambda req, now: outcome(
            Provider.SMS_8028, now, ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT, "timeout"
        ),
    )
    w = make_worker(repo, engine, clock, sms=sms)
    await w.run_once()
    r = await row(sf, m.id)
    assert r["sent_at"] is None and r["last_attempt_at"] == T0
    deadline = T0 + DEFAULT_POLICY.receipt_window
    await run_cleanup(repo, deadline - timedelta(seconds=1))
    assert (await row(sf, m.id))["recipient_fp_digest"] is not None
    await run_cleanup(repo, deadline)  # anchored on last_attempt_at
    r = await row(sf, m.id)
    assert r["recipient_fp_digest"] is None and r["status"] == "unknown"
    assert r["payload_fp_digest"] is not None
    clock.now = deadline
    await w.run_once()
    assert len(sms.calls) == 1


async def test_fingerprint_key_not_retired_while_records_need_it(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    await submit_broadcast(repo, ctx, 1)
    await make_worker(repo, engine, clock, sms=FakeSms(clock)).run_once()
    rotated = keys.rotated_fingerprint("f2").repo(sf)
    with pytest.raises(KeyRetirementRefused):
        await rotated.retire_key_version("fingerprint", "f1", T0)  # binding + recipient fp
    # Only after retention has released every f1 reference may f1 be retired.
    await run_cleanup(rotated, T0 + DEFAULT_POLICY.dedup_window)
    assert "f1" not in await rotated.fingerprint_key_versions_in_use(T0)
    await rotated.retire_key_version("fingerprint", "f1", T0)


async def test_cleanup_forever_survives_failures_and_resumes(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    async with sf() as s, s.begin():
        await s.execute(
            text(
                "UPDATE messages SET status='sending', lease_token=gen_random_uuid(),"
                " attempts=1, last_attempt_at=:t WHERE id=:i"
            ),
            {"t": T0, "i": m.id},
        )
        await s.execute(
            text("UPDATE messages SET status='unknown', failure_code='timeout' WHERE id=:i"),
            {"i": m.id},
        )
    calls = 0

    class Flaky:
        """First cycle: database outage. Second cycle: real repository."""

        async def recover_stale(self, now: Any) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("db down")
            return await repo.recover_stale(now)

        async def purge_due(self, now: Any, limit: int) -> int:
            return await repo.purge_due(now, limit)

    stop = asyncio.Event()
    task = asyncio.create_task(
        cleanup_forever(
            cast(PostgresMessageRepository, Flaky()),
            stop,
            interval=timedelta(milliseconds=20),
            clock=lambda: T0 + timedelta(hours=1),
        )
    )
    for _ in range(200):
        if await content_count(sf) == 0:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert calls >= 2 and await content_count(sf) == 0
