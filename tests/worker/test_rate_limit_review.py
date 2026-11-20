"""Regressions for docs/reviews/MSG-05-review.md (real PostgreSQL, deterministic).

P1-a: no nested pool checkout or lock-wait starvation; cancellation never leaks a lock.
P1-b: fresh shared time, skew cannot bypass the limit, waits bounded by lease/expiry,
      and actual provider-call times respect the limit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
    make_worker,
    submit_broadcast,
    submit_sms,
)

from oan_messaging.contracts import Channel, Claim
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_attempts, messages
from oan_messaging.worker import RateLimit, SharedRateLimiter, WorkerConfig
from oan_messaging.worker.dispatcher import CycleStats
from oan_messaging.worker.rate_limit import (
    RATE_LOCK_CLASS,
    Begun,
    DeadlineReached,
    RateLimited,
    clock_time_source,
    database_time,
)

LEASE = timedelta(minutes=2)


def sim(database: Any, limits: Any, clk: FakeClock | None = None, **kw: Any) -> SharedRateLimiter:
    """Limiter in an explicitly injected simulated time domain (tests only)."""
    return SharedRateLimiter(
        database, limits, time_source=clock_time_source(clk or FakeClock(T0)), **kw
    )


LOCKS_HELD = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :c AND granted"
)


async def rate_locks(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(LOCKS_HELD), {"c": RATE_LOCK_CLASS})).scalar_one())


async def wait_for_locks(engine: AsyncEngine, expected: int, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while await rate_locks(engine) != expected:
        if asyncio.get_running_loop().time() > end:
            raise AssertionError(f"rate lock count never reached {expected}")
        await asyncio.sleep(0.01)


@pytest.fixture
async def external_lock(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Another process holding the SMS rate lock."""
    conn = await engine.connect()
    await conn.execute(text("SELECT pg_advisory_lock(:c, 1)"), {"c": RATE_LOCK_CLASS})
    await conn.commit()
    yield conn
    await conn.execute(text("SELECT pg_advisory_unlock_all()"))
    await conn.commit()
    await conn.close()


class TimedSms(FakeSms):
    """Records the clock at the moment of each provider call."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.timeline: list[tuple[datetime, Any]] = []

    async def send(self, request: Any) -> Any:
        self.timeline.append((self.clock(), request.message_id))
        return await super().send(request)


async def call_times(sms: TimedSms) -> list[datetime]:
    return [t for t, _ in sms.timeline]


def max_in_any_window(times: list[datetime], window: timedelta) -> int:
    ts = sorted(times)
    return max((sum(1 for u in ts if t <= u < t + window) for t in ts), default=0)


# ------------------------------------------------------- P1-a: pools and locks


async def test_bounded_pool_progress_with_database_time(
    database_url: str,
    engine: AsyncEngine,  # truncates tables: isolation from earlier tests
    keys: Keys,
) -> None:
    small = make_engine(database_url, pool_size=1, max_overflow=0)
    sf = make_session_factory(small)
    repo = keys.repo(sf)
    try:
        async with small.connect() as conn:
            db_now = await database_time(conn)
        ctx = await make_caller(sf, keys)
        await submit_sms(repo, ctx, at=db_now)
        claim = (await repo.claim_due("w", db_now, LEASE, 1))[0]
        limiter = SharedRateLimiter(small, {Channel.SMS: RateLimit(1)})  # database time
        out = await asyncio.wait_for(limiter.begin_within_limit(repo, claim, db_now), 0.75)
        assert isinstance(out, Begun)
        # MSG-07b: the reservation runs inside the repository's own transaction, so a
        # one-connection pool suffices and the limiter owns no connections of its own.
        assert not hasattr(limiter, "coordination_engine")
    finally:
        await small.dispose()


async def test_single_connection_repository_pool_dispatches_everything(
    database_url: str,
    engine: AsyncEngine,  # truncates tables: isolation from earlier tests
    keys: Keys,
    clock: FakeClock,
) -> None:
    small = make_engine(database_url, pool_size=1, max_overflow=0)
    sf = make_session_factory(small)
    repo = keys.repo(sf)
    try:
        ctx = await make_caller(sf, keys)
        ids = {(await submit_broadcast(repo, ctx, i)).id for i in range(6)}
        sms = FakeSms(clock)
        w = make_worker(repo, small, clock, sms=sms, sms_limit=RateLimit(requests=100))
        stats = await asyncio.wait_for(w.run_once(), 10)
        assert stats.outcomes == {"sent": 6} and set(sms.counter) == ids
    finally:
        await small.dispose()


async def test_saturated_pool_with_more_workers_than_connections(
    database_url: str,
    engine: AsyncEngine,  # truncates tables: isolation from earlier tests
    keys: Keys,
    clock: FakeClock,
) -> None:
    """8 concurrent workers share a 2-connection repository pool and one rate lock:
    waiters hold no repository connection, so the lock owner always progresses."""
    small = make_engine(database_url, pool_size=2, max_overflow=0)
    sf = make_session_factory(small)
    try:
        ctx = await make_caller(sf, keys)
        repo = keys.repo(sf)
        ids = {(await submit_broadcast(repo, ctx, i)).id for i in range(24)}
        sms = FakeSms(clock)

        async def tick(_s: float) -> None:
            await asyncio.sleep(0.001)

        workers = [
            make_worker(
                keys.repo(sf),
                small,
                clock,
                sms=sms,
                worker_id=f"w{i}",
                batch_size=3,
                sms_limit=RateLimit(requests=1000),
                sleep=tick,
                lock_sleep=tick,
            )
            for i in range(8)
        ]
        for _ in range(5):
            await asyncio.wait_for(asyncio.gather(*(w.run_once() for w in workers)), 20)
            if set(sms.counter) == ids:
                break
            clock.advance(minutes=3)
        assert set(sms.counter) == ids and max(sms.counter.values()) == 1
    finally:
        await small.dispose()


async def test_cancel_while_waiting_for_lock_leaves_nothing_held(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    external_lock: AsyncConnection,
) -> None:
    ctx = await make_caller(sf, keys)
    await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    limiter = sim(
        engine, {Channel.SMS: RateLimit(1)}, lock_retry_interval=timedelta(milliseconds=5)
    )
    task = asyncio.create_task(limiter.begin_within_limit(repo, claim, T0))
    await asyncio.sleep(0.1)  # several failed try-locks
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await rate_locks(engine) == 1  # only the external holder
    await external_lock.execute(text("SELECT pg_advisory_unlock_all()"))
    await external_lock.commit()
    await wait_for_locks(engine, 0)
    out = await sim(engine, {Channel.SMS: RateLimit(1)}).begin_within_limit(repo, claim, T0)
    assert isinstance(out, Begun)


@pytest.mark.parametrize("phase", ["before-begin-commit", "after-begin-commit"])
async def test_cancel_while_holding_lock_releases_it(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """MSG-07b: the provider lock is a TRANSACTION lock taken inside ``begin_attempt``.
    Cancelled while holding it (before COMMIT), the transaction rolls back and the lock
    is gone; cancelled after COMMIT, the lock was already released by the commit."""
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    gate = asyncio.Event()
    inside = asyncio.Event()

    async def hanging_time(_conn: AsyncConnection) -> datetime:
        inside.set()  # the provider transaction lock is held here
        await gate.wait()
        return T0

    if phase == "before-begin-commit":
        limiter = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, time_source=hanging_time)
    else:
        real = repo.begin_attempt

        async def after_commit(c: Claim, now: datetime, **kw: Any) -> int:
            n = await real(c, now, **kw)  # committed
            inside.set()
            await gate.wait()
            return n

        monkeypatch.setattr(repo, "begin_attempt", after_commit)
        limiter = sim(engine, {Channel.SMS: RateLimit(1)})
    task = asyncio.create_task(limiter.begin_within_limit(repo, claim, T0))
    await asyncio.wait_for(inside.wait(), 5)
    assert await rate_locks(engine) == (1 if phase == "before-begin-commit" else 0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_for_locks(engine, 0)
    async with sf() as s:
        status = (
            await s.execute(select(messages.c.status).where(messages.c.id == m.id))
        ).scalar_one()
    assert status == ("accepted" if phase == "before-begin-commit" else "sending")


async def test_failure_inside_reservation_rolls_back_and_holds_no_lock(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    """Replaces the session-lock release test: an error while the transaction lock is
    held propagates, rolls the whole reservation back and leaves no lock behind."""
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)

    async def broken_time(_conn: AsyncConnection) -> datetime:
        raise RuntimeError("synthetic failure while holding the provider lock")

    limiter = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, time_source=broken_time)
    with pytest.raises(RuntimeError):
        await limiter.begin_within_limit(repo, claim, T0)
    await wait_for_locks(engine, 0)
    async with sf() as s:
        row = (
            await s.execute(
                select(messages.c.status, messages.c.attempts).where(messages.c.id == m.id)
            )
        ).one()
        assert tuple(row) == ("accepted", 0)
        assert (await s.execute(select(message_attempts))).first() is None


# ------------------------------------------------- P1-b: time, skew, deadlines


async def test_caller_clock_is_not_used_for_the_shared_bound(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    engine: AsyncEngine,
) -> None:
    ctx = await make_caller(sf, keys)
    for i in range(2):
        await submit_sms(repo, ctx, recipient=f"090000000{i}")
    claims = await repo.claim_due("w", T0, LEASE, 2)
    limiter = sim(engine, {Channel.SMS: RateLimit(1)})  # one shared domain (fixed at T0)
    assert isinstance(await limiter.begin_within_limit(repo, claims[0], T0), Begun)
    second = await limiter.begin_within_limit(repo, claims[1], T0 - timedelta(milliseconds=100))
    assert isinstance(second, RateLimited)


async def test_workers_with_skewed_clocks_share_one_bound(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await make_caller(sf, keys)
    for i in range(10):
        await submit_broadcast(repo, ctx, i)
    ahead, behind = FakeClock(T0 + timedelta(milliseconds=400)), FakeClock(T0)
    sms_a, sms_b = FakeSms(ahead), FakeSms(behind)

    async def frozen(_s: float) -> None:
        await asyncio.sleep(0)

    common: dict[str, Any] = {
        "sms_limit": RateLimit(requests=2),
        "sleep": frozen,
        "lock_sleep": frozen,
        "batch_size": 5,
    }
    wa = make_worker(repo, engine, ahead, sms=sms_a, worker_id="ahead", **common)
    wb = make_worker(keys.repo(sf), engine, behind, sms=sms_b, worker_id="behind", **common)
    await wa.run_once()
    await wb.run_once()  # 400 ms "earlier" clock must still see ahead's attempts
    assert len(sms_a.calls) + len(sms_b.calls) == 2


async def test_window_boundary_includes_dispatch_tolerance(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    external_lock: AsyncConnection,
) -> None:
    ctx = await make_caller(sf, keys)
    for i in range(3):
        await submit_broadcast(repo, ctx, i)
    c1, c2, c3 = await repo.claim_due("w", T0, LEASE, 3)
    await external_lock.execute(text("SELECT pg_advisory_unlock_all()"))
    await external_lock.commit()
    clk = FakeClock(T0)
    limiter = SharedRateLimiter(
        engine,
        {Channel.SMS: RateLimit(1)},
        time_source=clock_time_source(clk),
        monotonic=clk.monotonic,
        sleep=clk.sleep,
        dispatch_tolerance=timedelta(milliseconds=50),
    )

    async def at(c: Claim, t: datetime) -> Any:
        clk.now = t
        return await limiter.begin_within_limit(repo, c, t)

    t1 = T0 + timedelta(milliseconds=999)
    assert isinstance(await at(c1, t1), Begun)
    # Just past one window, but inside window + 2 x tolerance: still denied.
    denied = await at(c2, t1 + timedelta(milliseconds=1090))
    assert isinstance(denied, RateLimited)
    assert denied.retry_at == t1 + timedelta(milliseconds=1100)
    assert isinstance(await at(c2, t1 + timedelta(milliseconds=1100)), Begun)
    # Contention across the boundary: c3 starts waiting before the boundary while another
    # process holds the lock; once it gets the lock it is judged at its FRESH time.
    await external_lock.execute(text("SELECT pg_advisory_lock(:c, 1)"), {"c": RATE_LOCK_CLASS})
    await external_lock.commit()
    clk.now = t1 + timedelta(milliseconds=2150)  # c2's slot frees at t1 + 2200 ms
    task = asyncio.create_task(limiter.begin_within_limit(repo, c3, clk.now))
    await asyncio.sleep(0.05)
    assert not task.done()
    clk.now = t1 + timedelta(milliseconds=2200)  # time passes while waiting
    await external_lock.execute(text("SELECT pg_advisory_unlock_all()"))
    await external_lock.commit()
    out = await asyncio.wait_for(task, 5)
    assert isinstance(out, Begun) and out.begin_time >= t1 + timedelta(milliseconds=2200)


async def test_lock_wait_is_bounded_by_otp_expiry(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    external_lock: AsyncConnection,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_sms(repo, ctx)  # expires T0+5m
    clock.now = T0 + timedelta(minutes=5) - timedelta(milliseconds=50)
    sms = FakeSms(clock)
    w = make_worker(repo, engine, clock, sms=sms)  # lock retry sleep advances fake time
    stats = await asyncio.wait_for(w.run_once(), 10)
    assert sms.calls == [] and stats.expired == 1
    async with sf() as s:
        assert (
            await s.execute(select(messages.c.status).where(messages.c.id == m.id))
        ).scalar_one() == "expired"


async def test_lock_wait_is_bounded_by_lease(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    external_lock: AsyncConnection,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    clock.now = claim.lease_expires_at - timedelta(seconds=30, milliseconds=40)
    sms = FakeSms(clock)
    stats = CycleStats()
    w = make_worker(repo, engine, clock, sms=sms)
    await asyncio.wait_for(w._dispatch(claim, stats), 10)  # noqa: SLF001
    assert sms.calls == [] and stats.rate_limited_abandoned == 1
    async with sf() as s:
        assert (
            await s.execute(select(messages.c.status).where(messages.c.id == m.id))
        ).scalar_one() == "accepted"
    out = await sim(
        engine, {Channel.SMS: RateLimit(1)}, clock, monotonic=clock.monotonic, sleep=clock.sleep
    ).begin_within_limit(repo, claim, clock.now, deadline=clock.now + timedelta(milliseconds=30))
    assert isinstance(out, DeadlineReached)


@pytest.mark.parametrize("late_by", [timedelta(milliseconds=60), timedelta(seconds=2)])
async def test_late_start_after_begin_is_a_proven_non_send(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    late_by: timedelta,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    real = repo.begin_attempt

    async def slow_begin(c: Claim, now: datetime, **kw: Any) -> int:
        n = await real(c, now, **kw)
        clock.now = clock.now + late_by  # e.g. GC pause / slow commit after begin
        return n

    monkeypatch.setattr(repo, "begin_attempt", slow_begin)
    sms = FakeSms(clock)
    stats = await make_worker(repo, engine, clock, sms=sms).run_once()
    assert sms.calls == [] and stats.late_start_not_called == 1
    async with sf() as s:
        r = dict((await s.execute(select(messages).where(messages.c.id == m.id))).mappings().one())
        att = (await s.execute(select(message_attempts))).mappings().one()
    assert r["status"] == "accepted" and r["attempts"] == 1  # retry scheduled, not resent
    assert r["not_before"] == clock.now + timedelta(seconds=30)
    assert (att["outcome"], att["provider_status"]) == ("not_sent", "local_start_late")
    monkeypatch.undo()
    clock.advance(seconds=30)
    assert (await make_worker(repo, engine, clock, sms=sms).run_once()).outcomes == {"sent": 1}


async def test_expiry_passing_after_begin_expires_without_call(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_sms(repo, ctx)  # expires T0+5m
    clock.now = T0 + timedelta(minutes=4)
    real = repo.begin_attempt

    async def begin_then_tick(c: Claim, now: datetime, **kw: Any) -> int:
        n = await real(c, now, **kw)
        clock.now = clock.now + timedelta(seconds=61)
        return n

    monkeypatch.setattr(repo, "begin_attempt", begin_then_tick)
    sms = FakeSms(clock)
    await make_worker(repo, engine, clock, sms=sms).run_once()
    assert sms.calls == []
    async with sf() as s:
        st = (
            await s.execute(
                select(messages.c.status, messages.c.failure_code).where(messages.c.id == m.id)
            )
        ).one()
    assert tuple(st) == ("expired", "expired_before_send")


async def test_actual_provider_call_times_respect_shared_limit(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
) -> None:
    ctx = await make_caller(sf, keys)
    ids = {(await submit_broadcast(repo, ctx, i)).id for i in range(18)}
    sms = TimedSms(clock)
    limit = RateLimit(requests=3)
    w1 = make_worker(repo, engine, clock, sms=sms, worker_id="w1", sms_limit=limit, batch_size=6)
    w2 = make_worker(
        keys.repo(sf), engine, clock, sms=sms, worker_id="w2", sms_limit=limit, batch_size=6
    )
    for _ in range(40):
        await asyncio.gather(w1.run_once(), w2.run_once())
        if set(sms.counter) == ids:
            break
        clock.advance(minutes=3)
    assert set(sms.counter) == ids and max(sms.counter.values()) == 1
    assert max_in_any_window(await call_times(sms), timedelta(seconds=1)) <= 3


async def test_database_time_source_is_used_for_the_shared_bound(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    async with engine.connect() as conn:
        db_now = await database_time(conn)
    ctx = await make_caller(sf, keys)
    await submit_broadcast(repo, ctx, 1, at=db_now)
    await submit_broadcast(repo, ctx, 2, at=db_now)
    c1, c2 = await repo.claim_due("w", db_now, LEASE, 2)
    limiter = SharedRateLimiter(engine, {Channel.SMS: RateLimit(1)}, time_source=database_time)
    wrong_local = datetime(2001, 1, 1, tzinfo=UTC)  # ignored when a time source is set
    first = await limiter.begin_within_limit(repo, c1, wrong_local)
    assert isinstance(first, Begun) and abs(first.begin_time - db_now) < timedelta(seconds=5)
    assert isinstance(await limiter.begin_within_limit(repo, c2, wrong_local), RateLimited)


def test_timing_configuration_must_be_positive(engine: AsyncEngine) -> None:
    for bad in (
        {"lease_safety_margin": timedelta(0)},
        {"max_rate_wait": timedelta(0)},
        {"backoff": (timedelta(seconds=30), timedelta(0))},
    ):
        with pytest.raises(ValueError):
            WorkerConfig(**bad)
    for kw in (
        {"dispatch_tolerance": timedelta(0)},
        {"lock_retry_interval": timedelta(0)},
        {"max_concurrent_reservations": 0},
    ):
        with pytest.raises(ValueError):
            SharedRateLimiter(engine, **kw)


async def test_behind_clock_waits_instead_of_starting_before_a_recorded_start(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
) -> None:
    """Defensive guard for a MISCONFIGURED deployment that mixes time domains: a
    committed start lies 400 ms ahead of this limiter's source. Capacity is available,
    but beginning now would record the attempt later than the real call, so it waits."""
    ctx = await make_caller(sf, keys)
    for i in range(2):
        await submit_broadcast(repo, ctx, i)
    c1, c2 = await repo.claim_due("w", T0, LEASE, 2)
    ahead = SharedRateLimiter(
        engine,
        {Channel.SMS: RateLimit(2)},
        time_source=clock_time_source(FakeClock(T0 + timedelta(milliseconds=400))),
    )
    assert isinstance(await ahead.begin_within_limit(repo, c1, T0), Begun)
    behind = SharedRateLimiter(
        engine, {Channel.SMS: RateLimit(2)}, time_source=clock_time_source(FakeClock(T0))
    )
    out = await behind.begin_within_limit(repo, c2, T0)
    assert isinstance(out, RateLimited) and out.wait == timedelta(milliseconds=400)
    # Even a small backwards clock step must defer; never authorize in future DB time.
    near = SharedRateLimiter(
        engine,
        {Channel.SMS: RateLimit(2)},
        time_source=clock_time_source(FakeClock(T0 + timedelta(milliseconds=370))),
    )
    ok = await near.begin_within_limit(repo, c2, T0)
    assert isinstance(ok, RateLimited) and ok.wait == timedelta(milliseconds=30)
