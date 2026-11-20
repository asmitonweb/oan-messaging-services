"""Crash-safe retention cleanup (MSG-05 scheduling of the accepted ``purge_due``).

``run_cleanup`` drains ``purge_due`` in bounded batches until a batch touches nothing,
and recovers stale ``sending`` rows first. Every step is idempotent and conditional in
SQL, so it is safe to run on several workers at once and simply resumes after an outage.
The accepted storage enforces the retention rules:

* Content is purged for every status except accepted/sending, including sent and unknown.
* Bindings of accepted/sending/unknown messages are never released, however old.
* Completed bindings are released only after the guarantee window; GET still works.
* Recipient fingerprints are released at anchor + receipt window (anchor: sent_at, else
  last_attempt_at, else created_at).
* Receipt evidence is deleted after its retention period.

Key retirement is not automatic. ``retire_key_version`` refuses while any retained record
still references the version.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oan_messaging.storage.repository import PostgresMessageRepository

log = logging.getLogger(__name__)


@dataclass
class CleanupStats:
    recovered: int = 0
    touched: int = 0
    batches: int = 0
    complete: bool = False


async def run_cleanup(
    repo: PostgresMessageRepository, now: datetime, *, batch: int = 500, max_batches: int = 1000
) -> CleanupStats:
    if batch <= 0 or max_batches <= 0:
        raise ValueError("batch and max_batches must be positive")
    stats = CleanupStats(recovered=await repo.recover_stale(now))
    while stats.batches < max_batches:
        touched = await repo.purge_due(now, batch)
        stats.batches += 1
        stats.touched += touched
        if touched == 0:
            stats.complete = True
            break
    log.info(
        "cleanup recovered=%s touched=%s batches=%s complete=%s",
        stats.recovered,
        stats.touched,
        stats.batches,
        stats.complete,
    )
    return stats


async def cleanup_forever(
    repo: PostgresMessageRepository,
    stop: asyncio.Event,
    *,
    interval: timedelta,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Run every ``interval``. A failed cycle (e.g. DB outage) is logged by type and
    retried next interval; nothing is skipped because each cycle re-derives its work."""
    while not stop.is_set():
        try:
            await run_cleanup(repo, clock())
        except Exception as exc:
            log.error("cleanup cycle failed error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            pass
