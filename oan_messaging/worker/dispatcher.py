"""Durable delivery worker (MSG-05).

One ``run_once`` cycle:

1. ``recover_stale``: abandoned ``sending`` rows become ``unknown`` (never resent);
   abandoned claims that never began an attempt become claimable again.
2. ``claim_due``: a ``FOR UPDATE SKIP LOCKED`` lease in priority order, OTP first.
   Already-expired messages are expired there, before any claim.
3. For each claim, in order:

   a. Re-check expiry against the clock. If expired: ``mark_expired``, no call.
   b. ``SharedRateLimiter.begin_within_limit``. Rate-limit capacity is checked and
      ``begin_attempt`` commits ``sending`` + the attempt row, with expiry, lease and
      caller authorization re-checked, before any network call.
      ``DispatchNotAuthorized`` leaves the message held. ``MessageExpired`` leads to
      ``mark_expired``. ``LeaseLost`` means skip. If rate-limited, wait (bounded,
      within the lease) or give the claim back (``release_claim``), so the message
      is claimable again at once in priority order.
   c. Call the provider adapter, outside any database transaction, with an overall
      deadline that ends before the lease does (a timeout is ``unknown``).
   d. ``finish_attempt``. A retryable ``not_sent`` is rescheduled with backoff; the
      provider's ``retry_after`` always wins. Retries stop after ``max_attempts``.
      ``unknown`` is never retried.

The worker never converts ``unknown`` into another status, never resends after an
ambiguous outcome, and logs only ids, statuses and codes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from oan_messaging.contracts import (
    Channel,
    Claim,
    FailureCode,
    LeaseLost,
    MessageAdapter,
    ProviderOutcome,
    ProviderResult,
)
from oan_messaging.storage.errors import DispatchNotAuthorized, MessageExpired
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.worker.rate_limit import (
    BeginInterrupted,
    Begun,
    DeadlineReached,
    SharedRateLimiter,
)

log = logging.getLogger(__name__)

#: Backoff for provably-not-sent, retryable failures (attempt 1 -> 30 s, ...).
DEFAULT_BACKOFF: tuple[timedelta, ...] = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
)


@dataclass
class WorkerConfig:
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    batch_size: int = 1
    broadcast_enabled: bool = False
    #: SMS-03 broadcast ageing: a message accepted this long ago is claimed ahead of newer
    #: non-OTP traffic (OTPs still first), so a broadcast cannot be starved. None = off.
    broadcast_max_wait: timedelta | None = timedelta(seconds=180)
    lease: timedelta = timedelta(minutes=2)
    #: A provider call may only start while at least this much lease is left.
    lease_safety_margin: timedelta = timedelta(seconds=30)
    #: Lease kept back after a provider call for recording its result; the call's
    #: overall deadline is the remaining lease minus this. Default: 5 s, or half
    #: the safety margin when that is smaller.
    finish_reserve: timedelta | None = None
    #: Total attempts allowed for retryable NOT_SENT outcomes before giving up (failed).
    max_attempts: int = 5
    backoff: tuple[timedelta, ...] = DEFAULT_BACKOFF
    #: Bounded waiting for a rate-limit slot before abandoning a claim.
    max_rate_waits: int = 5
    max_rate_wait: timedelta = timedelta(seconds=2)

    def __post_init__(self) -> None:
        if self.batch_size <= 0 or self.max_attempts <= 0 or self.max_rate_waits < 0:
            raise ValueError("worker limits must be positive")
        zero = timedelta(0)
        if self.lease_safety_margin <= zero or self.max_rate_wait <= zero:
            raise ValueError("timing settings must be positive")
        if self.lease <= self.lease_safety_margin or not self.backoff:
            raise ValueError("lease must exceed the safety margin; backoff required")
        if self.broadcast_max_wait is not None and self.broadcast_max_wait <= zero:
            raise ValueError("broadcast_max_wait must be positive")
        if self.finish_reserve is None:
            self.finish_reserve = min(timedelta(seconds=5), self.lease_safety_margin / 2)
        elif not zero < self.finish_reserve < self.lease_safety_margin:
            raise ValueError("finish_reserve must be positive and below the safety margin")
        if any(step <= zero for step in self.backoff):
            raise ValueError("backoff steps must be positive")


@dataclass
class CycleStats:
    recovered: int = 0
    claimed: int = 0
    dispatched: int = 0
    expired: int = 0
    held: int = 0
    lease_lost: int = 0
    rate_limited_abandoned: int = 0
    late_start_not_called: int = 0
    begin_uncertain: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


class Worker:
    def __init__(
        self,
        repository: PostgresMessageRepository,
        adapters: Mapping[Channel, MessageAdapter],
        limiter: SharedRateLimiter,
        config: WorkerConfig | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        for channel, adapter in adapters.items():
            if adapter.channel is not channel:
                raise ValueError("adapter registered under the wrong channel")
        self._repo = repository
        self._adapters = dict(adapters)
        self._limiter = limiter
        self._cfg = config or WorkerConfig()
        self._clock = clock
        self._sleep = sleep

    @property
    def worker_id(self) -> str:
        return self._cfg.worker_id

    async def run_once(self) -> CycleStats:
        stats = CycleStats()
        now = self._clock()
        stats.recovered = await self._repo.recover_stale(now)
        claims = await self._repo.claim_due(
            self._cfg.worker_id,
            now,
            self._cfg.lease,
            self._cfg.batch_size,
            channels=tuple(self._adapters),
            allow_broadcast=self._cfg.broadcast_enabled,
            aged_after=self._cfg.broadcast_max_wait,
        )
        stats.claimed = len(claims)
        for claim in claims:
            await self._dispatch(claim, stats)
        return stats

    async def run_forever(
        self,
        stop: asyncio.Event,
        idle: float = 1.0,
        *,
        on_cycle: Callable[[], None] | None = None,
    ) -> None:
        """Run cycles until ``stop``. ``on_cycle`` runs after every cycle, successful or
        not (the runtime uses it for the liveness heartbeat): a loop that hangs stops
        calling it, a loop that keeps failing on the database keeps calling it."""
        if idle <= 0:
            raise ValueError("idle must be positive")
        failures = 0
        while not stop.is_set():
            try:
                stats = await self.run_once()
                failures = 0
                wait = idle if stats.claimed == 0 else 0.0
            except (SQLAlchemyError, OSError, TimeoutError) as exc:
                # Never log DB exception text/parameters. A failed cycle may have
                # committed sending: recovery decides; do not retry an adapter here.
                failures += 1
                log.error("worker cycle unavailable error_type=%s", type(exc).__name__)
                wait = min(30.0, idle * 2 ** min(failures - 1, 10))
            if on_cycle is not None:
                on_cycle()
            if wait:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)
                except TimeoutError:
                    pass

    # ----------------------------------------------------------------- dispatch

    def _expired(self, claim: Claim, now: datetime) -> bool:
        expires = claim.send_request.expires_at
        return expires is not None and expires <= now

    async def _expire(self, claim: Claim, now: datetime, stats: CycleStats) -> None:
        try:
            await self._repo.mark_expired(claim, now)
            stats.expired += 1
        except LeaseLost:
            stats.lease_lost += 1

    def _deadline(self, claim: Claim) -> datetime:
        """Latest worker-clock time at which a provider call may still start."""
        deadline = claim.lease_expires_at - self._cfg.lease_safety_margin
        expires = claim.send_request.expires_at
        return min(deadline, expires) if expires is not None else deadline

    async def _dispatch(self, claim: Claim, stats: CycleStats) -> None:
        mid = claim.message.id
        adapter = self._adapters.get(claim.message.channel)
        if adapter is None:
            # Misconfiguration: leave the claim to lapse; nothing was sent.
            log.error("no adapter for channel (message_id=%s)", mid)
            return

        deadline = self._deadline(claim)
        begun: Begun | None = None
        for _ in range(self._cfg.max_rate_waits + 1):
            now = self._clock()  # fresh after every wait
            if self._expired(claim, now):
                await self._expire(claim, now, stats)
                return
            if now >= deadline:
                break  # not enough lease left for a safe provider call
            try:
                started = await self._limiter.begin_within_limit(
                    self._repo, claim, now, deadline=deadline
                )
            except MessageExpired:
                await self._expire(claim, self._clock(), stats)
                return
            except DispatchNotAuthorized:
                stats.held += 1
                return
            except LeaseLost:
                stats.lease_lost += 1
                return
            if isinstance(started, Begun):
                begun = started
                break
            if isinstance(started, BeginInterrupted):
                # Commit of 'sending' is uncertain: never call the provider, never finish.
                # Uncommitted -> claim lapses (provably unsent); committed -> recover_stale
                # marks it unknown and it is never resent.
                stats.begin_uncertain += 1
                if self._expired(claim, self._clock()):
                    # Safe despite the uncertainty: mark_expired only matches a row that
                    # is still 'accepted' under our lease token (row lock waits for any
                    # in-flight begin); if 'sending' was committed it raises LeaseLost.
                    await self._expire(claim, self._clock(), stats)
                return
            if isinstance(started, DeadlineReached):
                break
            wait = min(started.wait, self._cfg.max_rate_wait)
            await self._sleep(max(wait.total_seconds(), 0.0))
        if begun is None:
            if self._expired(claim, self._clock()):
                await self._expire(claim, self._clock(), stats)
                return
            # Never began an attempt: provably unsent. Give the claim back so the
            # message is claimable again at once, in priority order; otherwise a
            # newer message could overtake it until the lease lapsed. If the release
            # fails, the lease lapses as before (recover_stale clears it).
            stats.rate_limited_abandoned += 1
            try:
                await self._repo.release_claim(claim, self._clock())
            except (SQLAlchemyError, OSError, TimeoutError) as exc:
                log.warning(
                    "claim release failed; lease will lapse (message_id=%s error_type=%s)",
                    mid,
                    type(exc).__name__,
                )
            log.info("rate limited; claim released (message_id=%s)", mid)
            return

        # Re-check current timing after begin, immediately before the provider call. If
        # the call can no longer start within the dispatch tolerance (which the shared
        # bound depends on), or the lease/expiry passed, do not call: record a provable
        # non-send through the legal NOT_SENT outcome.
        call_at = self._clock()
        elapsed = self._limiter.monotonic() - begun.mono_mark  # immune to wall-clock jumps
        late = elapsed > self._limiter.dispatch_tolerance.total_seconds()
        if self._expired(claim, call_at) or late or call_at >= deadline:
            await self._finish_unsent(
                claim, call_at, expired=self._expired(claim, call_at), stats=stats
            )
            return

        # Provider call: outside any database transaction, with an overall deadline
        # that ends before the lease does. Adapter timeouts are per phase, so a slow
        # provider could otherwise outlast the lease and lose a known result.
        reserve = self._cfg.finish_reserve or timedelta(0)  # set in __post_init__
        budget = (claim.lease_expires_at - call_at - reserve).total_seconds()
        try:
            result = await asyncio.wait_for(adapter.send(claim.send_request), timeout=budget)
        except TimeoutError:
            # The request may have reached the provider: ambiguous, never resent.
            at = self._clock()
            result = ProviderResult(
                outcome=ProviderOutcome.UNKNOWN,
                provider=adapter.provider,
                provider_status="call_deadline",
                failure_code=FailureCode.TIMEOUT,
                started_at=call_at,
                finished_at=at,
                detail="provider call exceeded the lease deadline",
            )
        except Exception as exc:  # contract says adapters never raise; stay conservative
            at = self._clock()
            result = ProviderResult(
                outcome=ProviderOutcome.UNKNOWN,
                provider=adapter.provider,
                provider_status="adapter_error",
                failure_code=FailureCode.INTERRUPTED,
                started_at=at,
                finished_at=at,
                detail=f"unexpected {type(exc).__name__}",
            )
        stats.dispatched += 1
        await self._finish(claim, result, stats)

    async def _finish_unsent(
        self, claim: Claim, at: datetime, *, expired: bool, stats: CycleStats
    ) -> None:
        provider = self._adapters[claim.message.channel].provider
        result = ProviderResult(
            outcome=ProviderOutcome.NOT_SENT,
            provider=provider,
            provider_status="expired" if expired else "local_start_late",
            failure_code=FailureCode.EXPIRED_BEFORE_SEND if expired else FailureCode.RATE_LIMITED,
            retryable=not expired,
            started_at=at,
            finished_at=at,
            detail="provider not called: start window missed"
            if not expired
            else "provider not called: expired after begin",
        )
        stats.late_start_not_called += 1
        # Same path as provider results: retry cap and persisted backoff apply.
        await self._finish(claim, result, stats)

    async def _finish(self, claim: Claim, result: ProviderResult, stats: CycleStats) -> None:
        mid = claim.message.id
        result = await self._bound_retries(claim, result)
        next_not_before = self._next_not_before(claim, result)
        try:
            # Bookkeeping only: bounded retries never repeat adapter.send. A lost
            # commit ACK is safe because finish is fenced and idempotent.
            for retry in range(3):
                try:
                    final = await asyncio.wait_for(
                        self._repo.finish_attempt(claim, result, next_not_before), timeout=1.0
                    )
                    break
                except (SQLAlchemyError, OSError, TimeoutError):
                    if retry == 2:
                        raise
                    await self._sleep(0.05 * (retry + 1))
        except LeaseLost:
            # Lease taken over (e.g. recovered as unknown): the stored state wins, and
            # nothing is resent.
            stats.lease_lost += 1
            log.warning(
                "finish_attempt lost the lease (message_id=%s outcome=%s)",
                mid,
                result.outcome.value,
            )
            return
        stats.outcomes[final.status.value] = stats.outcomes.get(final.status.value, 0) + 1
        log.info(
            "dispatched message_id=%s attempt=%s outcome=%s status=%s",
            mid,
            claim.send_request.attempt,
            result.outcome.value,
            final.status.value,
        )

    async def _bound_retries(self, claim: Claim, result: ProviderResult) -> ProviderResult:
        """Stop retry loops: after ``max_attempts`` a retryable NOT_SENT becomes final
        (still NOT_SENT, i.e. provably unsent -> failed). Never touches UNKNOWN."""
        if (
            result.outcome is ProviderOutcome.NOT_SENT
            and result.retryable
            and result.provider_status != "local_start_late"
            and await self._repo.provider_attempt_count(claim.message.id) >= self._cfg.max_attempts
        ):
            return ProviderResult.model_validate(
                result.model_dump() | {"retryable": False, "retry_after_seconds": None}
            )
        return result

    def _next_not_before(self, claim: Claim, result: ProviderResult) -> datetime | None:
        if result.outcome is not ProviderOutcome.NOT_SENT or not result.retryable:
            return None
        idx = min(claim.send_request.attempt - 1, len(self._cfg.backoff) - 1)
        # Repository takes max(this, finished_at + retry_after): provider waits win.
        return result.finished_at + self._cfg.backoff[idx]


__all__ = ["CycleStats", "Worker", "WorkerConfig"]
