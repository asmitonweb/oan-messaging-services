"""Worker dispatch: concurrency, leases, crashes, ambiguity, retries, expiry, rate limits,
priority and authorization. Real PostgreSQL; fake clock; fake providers."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import (
    T0,
    FakeClock,
    FakeSms,
    FakeTelegram,
    Keys,
    make_caller,
    make_worker,
    outcome,
    submit_broadcast,
    submit_sms,
    submit_telegram,
)

from oan_messaging.contracts import (
    Channel,
    FailureCode,
    Provider,
    ProviderOutcome,
    Purpose,
    SmsSendRequest,
    TelegramSendRequest,
)
from oan_messaging.storage.credentials import upsert_caller
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_attempts, message_content, messages
from oan_messaging.worker import RateLimit, WorkerConfig

LEASE = timedelta(minutes=2)


async def status_of(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> dict[str, Any]:
    async with sf() as s:
        return dict(
            (await s.execute(select(messages).where(messages.c.id == mid))).mappings().one()
        )


async def has_content(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> bool:
    async with sf() as s:
        return (
            await s.execute(
                select(message_content.c.message_id).where(message_content.c.message_id == mid)
            )
        ).first() is not None


async def attempt_times(sf: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sf() as s:
        return [
            r[0]
            for r in await s.execute(
                select(message_attempts.c.started_at).order_by(message_attempts.c.started_at)
            )
        ]


# ------------------------------------------------------------------- happy path


async def test_dispatch_sms_and_telegram_commit_before_call_and_purge(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    sms_msg = await submit_sms(repo, ctx)
    tg_msg = await submit_telegram(repo, ctx)
    seen: dict[str, Any] = {}

    async def observe(req: Any, now: Any) -> Any:
        # The attempt is committed (status sending + attempt row) before the call.
        seen[str(req.message_id)] = (await status_of(sf, req.message_id))["status"]
        from wsupport import sent

        return sent(
            Provider.SMS_8028 if isinstance(req, SmsSendRequest) else Provider.TELEGRAM,
            now,
            f"id-{req.message_id.hex[:6]}",
        )

    sms, tg = FakeSms(clock, observe), FakeTelegram(clock, observe)
    stats = await make_worker(repo, engine, clock, sms=sms, tg=tg).run_once()
    assert stats.claimed == 2 and stats.dispatched == 2 and stats.outcomes == {"sent": 2}
    assert seen == {str(sms_msg.id): "sending", str(tg_msg.id): "sending"}
    req = sms.calls[0]
    assert isinstance(req, SmsSendRequest) and req.recipient == "+251900000001"
    assert isinstance(tg.calls[0], TelegramSendRequest) and tg.calls[0].bot == "grievance"
    for mid in (sms_msg.id, tg_msg.id):
        r = await status_of(sf, mid)
        assert r["status"] == "sent" and r["content_purged_at"] is not None
        assert not await has_content(sf, mid)
    assert (await status_of(sf, tg_msg.id))["delivered_at"] is None  # never delivered


# ------------------------------------------------------ concurrency and leases


async def test_two_competing_workers_never_dispatch_the_same_claim(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    ids = {(await submit_broadcast(repo, ctx, i)).id for i in range(40)}
    sms = FakeSms(clock)  # shared fake provider sees every call from both workers
    # Both workers share one fake clock, and a worker waiting for the rate-limit lock
    # advances it while the other is between begin and call. With the 50 ms test window
    # that can push a call past the start window (a local non-send, retried later): a
    # timing artefact of the shared fake clock, not what this test checks. A wide window
    # keeps it about exclusivity.
    wide = timedelta(seconds=30)
    w1 = make_worker(
        repo, engine, clock, sms=sms, worker_id="w1", batch_size=25, dispatch_tolerance=wide
    )
    w2 = make_worker(
        keys.repo(sf),
        engine,
        clock,
        sms=sms,
        worker_id="w2",
        batch_size=25,
        dispatch_tolerance=wide,
    )
    s1, s2 = await asyncio.gather(w1.run_once(), w2.run_once())
    assert s1.claimed + s2.claimed == 40 and s1.claimed and s2.claimed
    assert set(sms.counter) == ids and max(sms.counter.values()) == 1
    async with sf() as s:
        owners = {r[0] for r in await s.execute(select(message_attempts.c.worker_id))}
    assert owners == {"w1", "w2"}


async def test_lease_expiry_lets_another_worker_take_over_without_double_send(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (stale,) = await repo.claim_due("w-stalled", T0, LEASE, 1)  # stalls before begin
    clock.advance(minutes=3)
    sms = FakeSms(clock)
    w2 = make_worker(repo, engine, clock, sms=sms, worker_id="w2")
    assert (await w2.run_once()).outcomes == {"sent": 1}
    from oan_messaging.worker.dispatcher import CycleStats

    # The stalled worker wakes up with its stale claim. With an honest clock its own lease
    # check stops it. With a skewed clock (T0+1m) the limiter's fresh shared time is raised
    # to w2's committed attempt (T0+3m), past the stale deadline, so it stops before storage.
    # Either way: no provider call, no state change.
    for stalled_clock in (clock, FakeClock(T0 + timedelta(minutes=1))):
        stalled = make_worker(repo, engine, stalled_clock, sms=sms, worker_id="w-stalled")
        stats = CycleStats()
        await stalled._dispatch(stale, stats)  # noqa: SLF001 - drive the stale claim
        assert stats.rate_limited_abandoned == 1 and stats.dispatched == 0
    assert sms.counter[m.id] == 1 and (await status_of(sf, m.id))["status"] == "sent"


async def test_taken_over_claim_gets_lease_lost_from_storage(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (stale,) = await repo.claim_due("w-stalled", T0, LEASE, 1)
    clock.advance(minutes=3)
    (fresh,) = await repo.claim_due("w2", clock.now, LEASE, 1)  # took over, not begun yet
    sms = FakeSms(clock)
    from oan_messaging.worker.dispatcher import CycleStats

    stats = CycleStats()
    skewed = make_worker(repo, engine, FakeClock(T0 + timedelta(minutes=1)), sms=sms)
    await skewed._dispatch(stale, stats)  # noqa: SLF001
    assert stats.lease_lost == 1 and sms.calls == []
    r = await status_of(sf, m.id)
    assert r["status"] == "accepted" and r["lease_token"] == fresh.claim_token


async def test_lease_too_short_for_a_safe_call_is_not_dispatched(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    clock.now = claim.lease_expires_at - timedelta(seconds=10)  # inside the safety margin
    sms = FakeSms(clock)
    from oan_messaging.worker.dispatcher import CycleStats

    stats = CycleStats()
    await make_worker(repo, engine, clock, sms=sms)._dispatch(claim, stats)  # noqa: SLF001
    assert sms.calls == [] and stats.rate_limited_abandoned == 1
    assert (await status_of(sf, m.id))["status"] == "accepted"


# ------------------------------------------------------------------- crashes


async def test_crash_after_begin_before_provider_call_recovers_as_unknown(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    """Crash between committing 'sending' and calling the provider: submission cannot be
    proven absent from the database alone, so it is unknown and never resent."""
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w-crashed", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)  # ... process dies here
    clock.advance(minutes=3)
    sms = FakeSms(clock)
    w = make_worker(repo, engine, clock, sms=sms)
    assert (await w.run_once()).recovered == 1
    r = await status_of(sf, m.id)
    assert (r["status"], r["failure_code"]) == ("unknown", "interrupted")
    assert not await has_content(sf, m.id)
    clock.advance(days=3)
    await w.run_once()
    assert sms.calls == []


async def test_crash_after_claim_before_begin_is_provably_unsent_and_retried(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    await repo.claim_due("w-crashed", T0, LEASE, 1)  # dies before begin_attempt
    clock.advance(minutes=3)
    sms = FakeSms(clock)
    assert (await make_worker(repo, engine, clock, sms=sms).run_once()).outcomes == {"sent": 1}
    assert sms.counter[m.id] == 1


async def test_crash_after_provider_call_before_finish_is_unknown_never_resent(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    calls = 0

    async def dies_after_gateway_accepts(req: Any, now: Any) -> Any:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError  # process killed after the gateway took it

    w = make_worker(repo, engine, clock, sms=FakeSms(clock, dies_after_gateway_accepts))
    try:
        await w.run_once()
    except asyncio.CancelledError:
        pass
    assert calls == 1 and (await status_of(sf, m.id))["status"] == "sending"
    clock.advance(minutes=3)
    healthy = FakeSms(clock)
    await make_worker(repo, engine, clock, sms=healthy).run_once()
    r = await status_of(sf, m.id)
    assert (r["status"], r["failure_code"]) == ("unknown", "interrupted")
    clock.advance(days=10)
    await make_worker(repo, engine, clock, sms=healthy).run_once()
    assert healthy.calls == []  # no blind resend, ever


async def test_restart_recovery_with_new_engine_and_worker(
    database_url: str,
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    msgs = [await submit_broadcast(repo, ctx, i) for i in range(1, 6)]
    (claim,) = await repo.claim_due("w-old", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    stuck_id = claim.message.id
    backlog = [m for m in msgs if m.id != stuck_id]
    await engine.dispose()  # old process gone
    eng2 = make_engine(database_url)
    try:
        sf2 = make_session_factory(eng2)
        clock.advance(minutes=3)
        sms = FakeSms(clock)
        stats = await make_worker(
            keys.repo(sf2), eng2, clock, sms=sms, worker_id="w-new"
        ).run_once()
        assert stats.recovered == 1 and stats.outcomes == {"sent": 4}
        assert (await status_of(sf2, stuck_id))["status"] == "unknown"
        assert set(sms.counter) == {b.id for b in backlog}
    finally:
        await eng2.dispose()


# -------------------------------------------------------- ambiguous outcomes


async def test_ambiguous_outcomes_are_unknown_and_never_resent(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    msgs = [await submit_broadcast(repo, ctx, i) for i in range(4)]
    cases = iter(
        [
            (FailureCode.TIMEOUT, "timeout"),
            (FailureCode.PROVIDER_SERVER_ERROR, "http_503"),
            (FailureCode.MALFORMED_RESPONSE, "http_200"),
            (FailureCode.RATE_LIMITED, "http_429"),
        ]
    )

    def ambiguous(req: Any, now: Any) -> Any:
        code, st = next(cases)
        return outcome(Provider.SMS_8028, now, ProviderOutcome.UNKNOWN, code, st)

    sms = FakeSms(clock, ambiguous)
    w = make_worker(repo, engine, clock, sms=sms)
    assert (await w.run_once()).outcomes == {"unknown": 3}
    # Shared cooldown protects the rest of the backlog, without retrying unknowns.
    assert len(sms.calls) == 3
    for _ in range(3):
        clock.advance(days=2)
        await w.run_once()
    assert len(sms.calls) == 4 and max(sms.counter.values()) == 1
    statuses = {(await status_of(sf, m.id))["provider_status"] for m in msgs}
    assert statuses == {"timeout", "http_503", "http_200", "http_429"}  # raw status kept


async def test_adapter_exception_is_treated_as_unknown(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)

    def boom(req: Any, now: Any) -> Any:
        raise RuntimeError("adapter bug with body Advisory 1")

    w = make_worker(repo, engine, clock, sms=FakeSms(clock, boom))
    assert (await w.run_once()).outcomes == {"unknown": 1}
    r = await status_of(sf, m.id)
    assert (r["failure_code"], r["provider_status"]) == ("interrupted", "adapter_error")


# --------------------------------------------------------------------- retries


async def test_retryable_not_sent_uses_persisted_backoff_and_stops_after_max_attempts(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)

    def refused(req: Any, now: Any) -> Any:
        return outcome(
            Provider.SMS_8028,
            now,
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_UNAVAILABLE,
            "conn_refused",
            retryable=True,
        )

    sms = FakeSms(clock, refused)
    w = make_worker(repo, engine, clock, sms=sms, max_attempts=3)
    await w.run_once()
    r = await status_of(sf, m.id)
    assert r["status"] == "accepted" and r["not_before"] == T0 + timedelta(seconds=30)
    assert await has_content(sf, m.id)  # still needed for the retry
    clock.advance(seconds=29)
    assert (await w.run_once()).claimed == 0
    clock.advance(seconds=1)
    await w.run_once()  # attempt 2 -> next in 2 min
    assert (await status_of(sf, m.id))["not_before"] == clock.now + timedelta(minutes=2)
    clock.advance(minutes=2)
    await w.run_once()  # attempt 3 == max -> final
    r = await status_of(sf, m.id)
    assert (r["status"], r["failure_code"], r["attempts"]) == ("failed", "provider_unavailable", 3)
    clock.advance(hours=5)
    await w.run_once()
    assert len(sms.calls) == 3  # no retry loop


async def test_provider_retry_after_wins_over_shorter_backoff(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_telegram(repo, ctx)
    first = True

    def flood_then_ok(req: Any, now: Any) -> Any:
        nonlocal first
        if first:
            first = False
            return outcome(
                Provider.TELEGRAM,
                now,
                ProviderOutcome.NOT_SENT,
                FailureCode.RATE_LIMITED,
                "tg_429",
                retryable=True,
                retry_after=120,
            )
        from wsupport import sent

        return sent(Provider.TELEGRAM, now, "4711")

    tg = FakeTelegram(clock, flood_then_ok)
    w = make_worker(repo, engine, clock, tg=tg)
    await w.run_once()
    assert (await status_of(sf, m.id))["not_before"] == T0 + timedelta(seconds=120)
    clock.advance(seconds=60)  # backoff (30 s) has passed, provider wait has not
    assert (await w.run_once()).claimed == 0
    clock.advance(seconds=60)
    assert (await w.run_once()).outcomes == {"sent": 1}
    assert len(tg.calls) == 2


async def test_huge_provider_retry_after_is_capped_and_worker_survives(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_telegram(repo, ctx)

    def absurd_wait(req: Any, now: Any) -> Any:
        return outcome(
            Provider.TELEGRAM,
            now,
            ProviderOutcome.NOT_SENT,
            FailureCode.RATE_LIMITED,
            "tg_429",
            retryable=True,
            retry_after=10**12,  # would overflow datetime arithmetic
        )

    w = make_worker(repo, engine, clock, tg=FakeTelegram(clock, absurd_wait))
    await w.run_once()
    assert (await status_of(sf, m.id))["not_before"] == T0 + timedelta(hours=1)
    async with sf() as s:
        cooldown = (
            await s.execute(
                text("SELECT cooldown_until FROM dispatch_controls WHERE channel = 'telegram'")
            )
        ).scalar_one()
    assert cooldown == T0 + timedelta(hours=1)


async def test_provider_call_past_the_lease_deadline_is_unknown(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)

    async def trickle() -> Any:
        await asyncio.sleep(30)  # a provider that never finishes in time
        raise AssertionError("deadline did not fire")

    sms = FakeSms(clock, lambda req, now: trickle())
    w = make_worker(
        repo,
        engine,
        clock,
        sms=sms,
        lease=timedelta(seconds=2),
        lease_safety_margin=timedelta(seconds=1),
        finish_reserve=timedelta(milliseconds=500),
    )
    assert (await w.run_once()).outcomes == {"unknown": 1}
    r = await status_of(sf, m.id)
    assert (r["status"], r["failure_code"], r["provider_status"]) == (
        "unknown",
        "timeout",
        "call_deadline",
    )
    assert len(sms.calls) == 1


async def test_retry_wait_reaching_expiry_expires_without_sending(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_sms(repo, ctx)  # OTP expires T0+5m

    def flood(req: Any, now: Any) -> Any:
        return outcome(
            Provider.SMS_8028,
            now,
            ProviderOutcome.NOT_SENT,
            FailureCode.RATE_LIMITED,
            "x",
            retryable=True,
            retry_after=600,
        )

    sms = FakeSms(clock, flood)
    w = make_worker(repo, engine, clock, sms=sms)
    await w.run_once()
    r = await status_of(sf, m.id)
    assert (r["status"], r["failure_code"]) == ("expired", "expired_before_send")
    clock.advance(minutes=20)
    await w.run_once()
    assert len(sms.calls) == 1


# ---------------------------------------------------------------------- expiry


async def test_otp_expired_while_queued_after_outage_is_never_sent(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    otps = [await submit_sms(repo, ctx, recipient=f"090000010{i}") for i in range(3)]
    later = await submit_broadcast(repo, ctx, 9)
    clock.advance(minutes=30)  # outage longer than the OTP lifetime
    sms = FakeSms(clock)
    await make_worker(repo, engine, clock, sms=sms).run_once()
    for o in otps:
        r = await status_of(sf, o.id)
        assert r["status"] == "expired" and not await has_content(sf, o.id)
    assert set(sms.counter) == {later.id}


async def test_otp_expiring_immediately_before_dispatch_is_not_sent(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    a = await submit_sms(repo, ctx, recipient="0900000001", expires_in=timedelta(minutes=10))
    b = await submit_sms(
        repo,
        ctx,
        at=T0 + timedelta(seconds=1),
        recipient="0900000002",
        expires_in=timedelta(minutes=5) - timedelta(seconds=1),
    )
    clock.now = T0 + timedelta(minutes=5) - timedelta(milliseconds=500)
    sms = FakeSms(clock)
    # One SMS per second: b waits for the slot; the injected sleep advances fake time
    # past b's expiry, which is re-checked immediately before dispatch.
    w = make_worker(repo, engine, clock, sms=sms, sms_limit=RateLimit(requests=1))
    stats = await w.run_once()
    assert set(sms.counter) == {a.id} and stats.expired == 1
    assert (await status_of(sf, b.id))["status"] == "expired"


# ----------------------------------------------------- rate limits and priority


async def test_rate_limit_is_shared_across_workers(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    for i in range(30):
        await submit_broadcast(repo, ctx, i)
    sms = FakeSms(clock)
    limit = RateLimit(requests=5)

    async def no_time_passes(_s: float) -> None:
        await asyncio.sleep(0)

    w1 = make_worker(
        repo,
        engine,
        clock,
        sms=sms,
        worker_id="w1",
        sms_limit=limit,
        sleep=no_time_passes,
        batch_size=15,
    )
    w2 = make_worker(
        keys.repo(sf),
        engine,
        clock,
        sms=sms,
        worker_id="w2",
        sms_limit=limit,
        sleep=no_time_passes,
        batch_size=15,
    )
    s1, s2 = await asyncio.gather(w1.run_once(), w2.run_once())
    assert len(sms.calls) == 5  # not 5 per worker
    assert s1.rate_limited_abandoned + s2.rate_limited_abandoned == 25
    # Time moves on; abandoned (never-begun) claims lapse and are dispatched later, and no
    # one-second window ever exceeds the shared limit.
    for _ in range(12):
        clock.advance(minutes=3)
        await asyncio.gather(w1.run_once(), w2.run_once())
    assert len(sms.calls) == 30 and max(sms.counter.values()) == 1
    times = await attempt_times(sf)
    for t in times:
        assert sum(1 for u in times if t - timedelta(seconds=1) < u <= t) <= 5


async def test_rate_limited_claim_is_released_and_keeps_its_place(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    first = await submit_sms(repo, ctx, recipient="0900000301")
    older = await submit_sms(repo, ctx, at=T0 + timedelta(milliseconds=1), recipient="0900000302")
    sms = FakeSms(clock)

    async def frozen(_s: float) -> None:
        await asyncio.sleep(0)

    w = make_worker(repo, engine, clock, sms=sms, sleep=frozen, sms_limit=RateLimit(requests=1))
    stats = await w.run_once()
    assert [c.message_id for c in sms.calls] == [first.id]
    assert stats.rate_limited_abandoned == 1
    r = await status_of(sf, older.id)
    # Given back at once: no lease left to wait out.
    assert (r["status"], r["lease_token"], r["lease_expires_at"]) == ("accepted", None, None)
    clock.advance(seconds=2)  # the next one-second window
    newer = await submit_sms(repo, ctx, at=clock.now, recipient="0900000303")
    await w.run_once()
    # The older OTP goes before the newer one, without waiting for a lease to lapse.
    assert [c.message_id for c in sms.calls][1] == older.id
    assert sms.counter[newer.id] == 0


async def test_otp_priority_and_reserved_capacity(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    bulk = [await submit_broadcast(repo, ctx, i) for i in range(10)]
    clock.advance(seconds=1)
    otps = [await submit_sms(repo, ctx, at=clock.now, recipient=f"090000020{i}") for i in range(2)]
    sms = FakeSms(clock)

    async def frozen(_s: float) -> None:
        await asyncio.sleep(0)

    w = make_worker(
        repo, engine, clock, sms=sms, sleep=frozen, sms_limit=RateLimit(requests=5, otp_reserve=2)
    )
    await w.run_once()
    sent_ids = [c.message_id for c in sms.calls]
    assert set(sent_ids[:2]) == {o.id for o in otps}  # queued later, dispatched first
    # Non-OTP traffic may only fill the window up to 5 - 2 = 3 slots in total, so after
    # the two OTPs exactly one broadcast goes out and two slots stay free for OTP.
    assert len([i for i in sent_ids if i in {b.id for b in bulk}]) == 1
    assert len(sent_ids) == 3
    # Same window: broadcasts are blocked, but a new OTP still gets a reserved slot.
    async with sf() as s, s.begin():
        await s.execute(
            text(
                "UPDATE messages SET lease_token = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE status = 'accepted'"
            )
        )
    late_otp = await submit_sms(repo, ctx, at=clock.now, recipient="0900000299")
    await w.run_once()
    assert sms.counter[late_otp.id] == 1
    assert len([c for c in sms.calls if c.message_id in {b.id for b in bulk}]) == 1


# --------------------------------------------------------------- authorization


async def test_unauthorized_dispatch_is_held_not_sent(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await upsert_caller(
        sf, "svc", channels=frozenset(Channel), purposes=frozenset(Purpose), enabled=False
    )
    sms = FakeSms(clock)
    w = make_worker(repo, engine, clock, sms=sms)
    from oan_messaging.worker.dispatcher import CycleStats

    stats = CycleStats()
    await w._dispatch(claim, stats)  # noqa: SLF001 - revoked between claim and begin
    assert stats.held == 1 and sms.calls == []
    assert (await status_of(sf, m.id))["status"] == "accepted"
    await upsert_caller(sf, "svc", channels=frozenset(Channel), purposes=frozenset(Purpose))
    assert (await w.run_once()).outcomes == {"sent": 1}


def test_worker_config_validation() -> None:
    import pytest

    for bad in (
        {"batch_size": 0},
        {"max_attempts": 0},
        {"lease": timedelta(seconds=10)},
        {"backoff": ()},
        {"finish_reserve": timedelta(seconds=30)},
        {"finish_reserve": timedelta(0)},
        {"broadcast_max_wait": timedelta(0)},
    ):
        with pytest.raises(ValueError):
            WorkerConfig(**bad)
    with pytest.raises(ValueError):
        RateLimit(requests=0)
    with pytest.raises(ValueError):
        RateLimit(requests=2, otp_reserve=2)


async def test_worker_logs_contain_no_text_recipients_or_secrets(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    caplog: Any,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    ctx = await make_caller(sf, keys)
    await submit_sms(repo, ctx)  # OTP text "Your OAN code is 482913."
    await submit_telegram(repo, ctx)
    results = iter([ProviderOutcome.UNKNOWN, ProviderOutcome.NOT_SENT])

    def mixed(req: Any, now: Any) -> Any:
        kind = next(results)
        prov = Provider.SMS_8028 if isinstance(req, SmsSendRequest) else Provider.TELEGRAM
        return outcome(
            prov,
            now,
            kind,
            FailureCode.TIMEOUT
            if kind is ProviderOutcome.UNKNOWN
            else FailureCode.PROVIDER_UNAVAILABLE,
            "x",
            retryable=kind is ProviderOutcome.NOT_SENT,
        )

    await make_worker(
        repo, engine, clock, sms=FakeSms(clock, mixed), tg=FakeTelegram(clock, mixed)
    ).run_once()
    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    for needle in (
        "482913",
        "Your OAN code",
        "0900000001",
        "+251900000001",
        "555000111",
        "Grievance updated",
        keys.pepper.hex(),
    ):
        assert needle not in logged
    assert "dispatched message_id=" in logged
