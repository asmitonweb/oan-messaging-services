"""Regressions for docs/reviews/MSG-05-v2-review.md (real PostgreSQL).

P1: the shared bound uses ONE authoritative time domain (database time by default);
    skew larger than the window, in either order, cannot open a window early; lateness
    is measured on a monotonic clock.
P1: the deadline bounds the whole call (semaphore, connect, SQL, begin, sleeps); a
    cancelled/uncertain begin never authorizes a provider call.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import (
    T0,
    FakeClock,
    FakeSms,
    Keys,
    make_caller,
    submit_broadcast,
    submit_sms,
)

from oan_messaging.contracts import Channel, Claim
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import messages
from oan_messaging.worker import RateLimit, SharedRateLimiter, Worker, WorkerConfig
from oan_messaging.worker.dispatcher import CycleStats
from oan_messaging.worker.rate_limit import (
    RATE_LOCK_CLASS,
    BeginInterrupted,
    Begun,
    DeadlineReached,
    RateLimited,
    clock_time_source,
    database_time,
)

LEASE = timedelta(minutes=2)
LOCKS = "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND classid=:c AND granted"


async def db_now(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        return await database_time(conn)


async def rate_locks(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(LOCKS), {"c": RATE_LOCK_CLASS})).scalar_one())


async def until_no_locks(engine: AsyncEngine, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while await rate_locks(engine):
        if time.monotonic() > end:
            raise AssertionError("rate lock leaked")
        await asyncio.sleep(0.01)


async def status(sf: async_sessionmaker[AsyncSession], mid: Any) -> tuple[str, int]:
    async with sf() as s:
        row = (
            await s.execute(
                select(messages.c.status, messages.c.attempts).where(messages.c.id == mid)
            )
        ).one()
    return str(row[0]), int(row[1])


def default_limiter(engine: AsyncEngine, limit: int = 1, **kw: Any) -> SharedRateLimiter:
    """Production default: database time, real monotonic clock."""
    return SharedRateLimiter(engine, {Channel.SMS: RateLimit(limit)}, **kw)


# ---------------------------------------------------- P1: authoritative time


def test_database_time_is_the_default_time_source(engine: AsyncEngine) -> None:
    lim = SharedRateLimiter(engine)
    assert lim._time_source is database_time  # noqa: SLF001


@pytest.mark.parametrize("order", ["ahead-first", "behind-first"])
async def test_skew_larger_than_window_cannot_open_a_window_early(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    order: str,
) -> None:
    now = await db_now(engine)
    ctx = await make_caller(sf, keys)
    for i in range(2):
        await submit_sms(repo, ctx, at=now, recipient=f"090000000{i}")
    c1, c2 = await repo.claim_due("w", now, LEASE, 2)
    ahead, behind = now + timedelta(seconds=2), now - timedelta(seconds=2)  # skew > window
    first_now, second_now = (ahead, behind) if order == "ahead-first" else (behind, ahead)
    a, b = default_limiter(engine), default_limiter(engine)  # two independent instances
    try:
        assert isinstance(await a.begin_within_limit(repo, c1, first_now), Begun)
        assert isinstance(await b.begin_within_limit(repo, c2, second_now), RateLimited)
    finally:
        await a.aclose()
        await b.aclose()


async def test_reviewer_v2_probe_semantics_with_one_shared_domain(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    """The v2 probe's scenario (a second limiter called with T0 + 2 s) in a consistent
    domain: both limiters share the injected source, so the caller's T0 + 2 s is ignored."""
    ctx = await make_caller(sf, keys)
    for i in range(2):
        await submit_sms(repo, ctx, recipient=f"090000000{i}")
    claims = await repo.claim_due("w", T0, LEASE, 2)
    shared = clock_time_source(FakeClock(T0))
    a = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, time_source=shared)
    b = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, time_source=shared)
    assert isinstance(await a.begin_within_limit(repo, claims[0], T0), Begun)
    assert isinstance(
        await b.begin_within_limit(repo, claims[1], T0 + timedelta(seconds=2)), RateLimited
    )


class MonoSms(FakeSms):
    """Fake provider that records the REAL monotonic time of every call."""

    def __init__(self, wall: Callable[[], datetime]) -> None:
        super().__init__(FakeClock())
        self.wall = wall
        self.mono: list[float] = []

    async def send(self, request: Any) -> Any:
        self.mono.append(time.monotonic())
        self.clock.now = self.wall()
        return await super().send(request)


async def test_skewed_workers_real_call_spacing_with_database_time(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    """Two workers whose wall clocks differ by 4 s (more than the 1 s window), sharing a
    2/s limit through database time: real monotonic call spacing never exceeds 2/s.

    On a slow runner the gap between reserving a slot and the call can pass the 200 ms
    dispatch tolerance; the worker then records a late start and backs the message off
    (30 s by default), which outlasted the old 20 s deadline (CI, 20 Nov 2026). A 1 s
    backoff and a 60 s deadline let such a message be retried; the checks are unchanged:
    every message sent exactly once, never more than 2 calls in any real second."""
    start = await db_now(engine)
    ctx = await make_caller(sf, keys)
    ids = {
        (await submit_broadcast(repo, ctx, i, at=start - timedelta(seconds=3))).id for i in range(6)
    }
    skews = (timedelta(seconds=2), timedelta(seconds=-2))
    sms = MonoSms(lambda: datetime.now(UTC))
    workers = []
    for i, skew in enumerate(skews):

        def wall(skew: timedelta = skew) -> datetime:
            return datetime.now(UTC) + skew

        limiter = SharedRateLimiter(engine, {Channel.SMS: RateLimit(2)})  # database time
        workers.append(
            Worker(
                keys.repo(sf),
                {Channel.SMS: sms},
                limiter,
                WorkerConfig(
                    broadcast_enabled=True,
                    worker_id=f"w{i}",
                    batch_size=3,
                    backoff=(timedelta(seconds=1),),  # a late start retries quickly
                ),
                clock=wall,
            )
        )
    deadline = time.monotonic() + 60
    while set(sms.counter) != ids and time.monotonic() < deadline:
        await asyncio.gather(*(w.run_once() for w in workers))
    assert set(sms.counter) == ids and max(sms.counter.values()) == 1
    calls = sorted(sms.mono)
    worst = max(sum(1 for u in calls if t <= u < t + 1.0) for t in calls)
    assert worst <= 2, calls


async def test_lateness_uses_monotonic_time_not_wall_clock(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await make_caller(sf, keys)
    a = await submit_broadcast(repo, ctx, 1)
    b = await submit_broadcast(repo, ctx, 2)
    wall = FakeClock(T0)
    mono = {"t": 1000.0}
    limiter = SharedRateLimiter(
        engine,
        {Channel.SMS: RateLimit(100)},
        time_source=clock_time_source(wall),
        monotonic=lambda: mono["t"],
    )
    sms = FakeSms(wall)
    worker = Worker(
        repo,
        {Channel.SMS: sms},
        limiter,
        WorkerConfig(broadcast_enabled=True, batch_size=1),
        clock=wall,
    )
    real = repo.begin_attempt
    jumps = iter([(-5.0, 2.0), (10.0, 0.001)])  # (wall jump s, monotonic elapsed s)

    async def begin_then_jump(c: Claim, now: datetime, **kw: Any) -> int:
        n = await real(c, now, **kw)
        w_jump, m_elapsed = next(jumps)
        wall.now = wall.now + timedelta(seconds=w_jump)
        mono["t"] += m_elapsed
        return n

    monkeypatch.setattr(repo, "begin_attempt", begin_then_jump)
    # 1) Wall clock jumps BACK 5 s while 2 s really elapse: still detected as late.
    s1 = await worker.run_once()
    assert s1.late_start_not_called == 1 and sms.calls == []
    # 2) Wall clock jumps FORWARD 10 s while 1 ms elapses: not late; the call proceeds.
    wall.now = wall.now + timedelta(minutes=1)
    s2 = await worker.run_once()
    assert s2.late_start_not_called == 0 and len(sms.calls) == 1
    # The late message was retried (proven non-send, then backoff) and sent on attempt 2;
    # the other was never claimed (batch size 1).
    assert sorted([await status(sf, a.id), await status(sf, b.id)]) == [
        ("accepted", 0),
        ("sent", 2),
    ]


# ------------------------------------------------- P1: deadline over the whole call


@pytest.mark.parametrize("source", ["database", "simulated"])
async def test_semaphore_saturation_obeys_deadline(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    source: str,
) -> None:
    now = await db_now(engine) if source == "database" else T0
    ctx = await make_caller(sf, keys)
    m = await submit_sms(repo, ctx, at=now)
    (claim,) = await repo.claim_due("w", now, LEASE, 1)
    kw: dict[str, Any] = {"max_concurrent_reservations": 1}
    if source == "simulated":
        kw["time_source"] = clock_time_source(FakeClock(T0))
    lim = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, **kw)
    await lim._slots.acquire()  # noqa: SLF001 - another task holds the only slot
    try:
        t = time.monotonic()
        out = await asyncio.wait_for(
            lim.begin_within_limit(repo, claim, now, deadline=now + timedelta(milliseconds=50)), 0.3
        )
        assert isinstance(out, DeadlineReached) and time.monotonic() - t < 0.25
    finally:
        lim._slots.release()  # noqa: SLF001
        await lim.aclose()
    assert await status(sf, m.id) == ("accepted", 0)


async def test_slow_connection_obeys_deadline(
    database_url: str,
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
) -> None:
    """MSG-07b: the reservation uses a repository connection. When none is available
    (the only pooled connection is busy), the deadline still bounds the wait and nothing
    begins."""
    now = await db_now(engine)
    ctx = await make_caller(sf, keys)
    m = await submit_sms(keys.repo(sf), ctx, at=now)
    small = make_engine(database_url, pool_size=1, max_overflow=0)
    try:
        repo = keys.repo(make_session_factory(small))
        (claim,) = await repo.claim_due("w", now, LEASE, 1)
        busy = await small.connect()  # holds the only connection
        try:
            lim = default_limiter(small)
            t = time.monotonic()
            out = await lim.begin_within_limit(
                repo, claim, now, deadline=now + timedelta(milliseconds=50)
            )
            assert isinstance(out, DeadlineReached) and time.monotonic() - t < 0.5
        finally:
            await busy.close()
    finally:
        await small.dispose()
    await until_no_locks(engine)
    assert await status(sf, m.id) == ("accepted", 0)


@pytest.mark.parametrize("where", ["time-source", "count-query"])
async def test_slow_sql_while_holding_lock_obeys_deadline_and_releases(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    now = await db_now(engine)
    ctx = await make_caller(sf, keys)
    m = await submit_sms(repo, ctx, at=now)
    (claim,) = await repo.claim_due("w", now, LEASE, 1)

    async def slow_db_time(conn: AsyncConnection) -> datetime:
        await conn.execute(text("SELECT pg_sleep(1)"))
        return await database_time(conn)

    if where == "time-source":
        lim = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, time_source=slow_db_time)
    else:
        lim = default_limiter(engine)
        real_execute = AsyncConnection.execute

        async def slow_count(self: AsyncConnection, stmt: Any, *a: Any, **k: Any) -> Any:
            if "FROM message_attempts" in str(stmt):
                await real_execute(self, text("SELECT pg_sleep(1)"))
            return await real_execute(self, stmt, *a, **k)

        monkeypatch.setattr(AsyncConnection, "execute", slow_count)
    t = time.monotonic()
    out = await lim.begin_within_limit(repo, claim, now, deadline=now + timedelta(milliseconds=100))
    elapsed = time.monotonic() - t
    monkeypatch.undo()
    assert isinstance(out, DeadlineReached) and elapsed < 0.8
    await until_no_locks(engine)  # the lock-holding session was closed
    assert await status(sf, m.id) == ("accepted", 0)


@pytest.mark.parametrize("phase", ["before-commit", "after-commit"])
async def test_slow_begin_is_interrupted_and_never_called_or_duplicated(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    clock = FakeClock(T0)
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    real = repo.begin_attempt

    async def slow_begin(c: Claim, now: datetime, **kw: Any) -> int:
        if phase == "after-commit":
            n = await real(c, now, **kw)
            await asyncio.sleep(1.0)  # commit done, acknowledgement slow
            return n
        await asyncio.sleep(1.0)
        return await real(c, now, **kw)

    monkeypatch.setattr(repo, "begin_attempt", slow_begin)
    limiter = SharedRateLimiter(
        engine, {Channel.SMS: RateLimit(10)}, time_source=clock_time_source(clock)
    )
    sms = FakeSms(clock)
    # Lease 2 min, safety margin chosen so only ~100 ms of real budget remain.
    worker = Worker(
        repo,
        {Channel.SMS: sms},
        limiter,
        WorkerConfig(
            broadcast_enabled=True,
            lease=timedelta(seconds=31),
            batch_size=1,
            lease_safety_margin=timedelta(seconds=30, milliseconds=900),
        ),
        clock=clock,
    )
    stats = await worker.run_once()
    assert sms.calls == []  # never authorized a call
    if phase == "before-commit":
        # MSG-07b: COMMIT was never issued, so nothing began (provably), not uncertain.
        assert (stats.begin_uncertain, stats.rate_limited_abandoned) == (0, 1)
    else:
        assert stats.begin_uncertain == 1
    await until_no_locks(engine)
    st = await status(sf, m.id)
    assert st == (("accepted", 0) if phase == "before-commit" else ("sending", 1))
    # Recovery: an uncommitted begin is re-claimable and sent once; a committed one is
    # recovered as unknown and never resent.
    monkeypatch.undo()
    clock.advance(minutes=3)
    await Worker(
        repo, {Channel.SMS: sms}, limiter, WorkerConfig(broadcast_enabled=True), clock=clock
    ).run_once()
    if phase == "before-commit":
        assert sms.counter[m.id] == 1 and (await status(sf, m.id))[0] == "sent"
    else:
        assert sms.calls == [] and (await status(sf, m.id))[0] == "unknown"


async def test_otp_expiry_deadline_bounds_a_slow_begin_with_database_time(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = await db_now(engine)
    ctx = await make_caller(sf, keys)
    m = await submit_sms(repo, ctx, at=start - timedelta(minutes=5) + timedelta(milliseconds=300))
    real = repo.begin_attempt

    async def slow_begin(c: Claim, now: datetime, **kw: Any) -> int:
        await asyncio.sleep(1.0)
        return await real(c, now, **kw)

    monkeypatch.setattr(repo, "begin_attempt", slow_begin)
    sms = FakeSms(FakeClock())
    worker = Worker(
        repo,
        {Channel.SMS: sms},
        default_limiter(engine, 10),
        WorkerConfig(
            broadcast_enabled=True,
        ),
        clock=lambda: datetime.now(UTC),
    )
    t = time.monotonic()
    stats = await worker.run_once()
    assert time.monotonic() - t < 1.0  # bounded by the ~300 ms left before expiry
    assert sms.calls == [] and stats.begin_uncertain + stats.expired >= 1
    await until_no_locks(engine)
    await asyncio.sleep(0.5)
    monkeypatch.undo()
    await worker.run_once()  # now past expiry: expired, never sent
    assert sms.calls == [] and (await status(sf, m.id))[0] == "expired"


def test_cycle_stats_reports_uncertain_begins() -> None:
    assert CycleStats().begin_uncertain == 0
    assert BeginInterrupted() == BeginInterrupted()
