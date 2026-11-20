"""Statistics snapshot behind GET /api/v1/charts.

The cleanup component rebuilds it every ``stats_refresh_seconds`` (default 15 minutes),
as the other OAN dashboard services do (A2C, Grievance): a scheduled rebuild under a
database lock, ``as_of`` on every response, and no dashboard request that aggregates the
messages table.

* A message is counted on the day it was accepted (Ethiopian calendar day,
  Africa/Addis_Ababa), with its current status. Statuses still change for up to the
  receipt window (7 days), so each refresh rebuilds the last ``REFRESH_DAYS`` days; older
  days are final. The first refresh on a database builds every day.
* Only counts and latency histograms are stored: no recipient, text or reference.
* Campaigns are alert and broadcast messages of one caller that share the first two
  ``:``-separated parts of their ``source_reference`` (``da_broadcast:<broadcast>:<who>``
  gives ``da_broadcast:<broadcast>``), with at least two recipients.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.storage.schema import stats_campaigns, stats_daily, stats_failures, stats_state

log = logging.getLogger(__name__)

EAT = ZoneInfo("Africa/Addis_Ababa")
#: Latency bucket edges in seconds. Bucket 0 is under 1 s; bucket i (1..12) is
#: [EDGES[i-1], EDGES[i]); the last bucket is a day or more.
BUCKET_EDGES_S: tuple[int, ...] = (1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 21600, 86400)
N_BUCKETS = len(BUCKET_EDGES_S) + 1
#: Days rebuilt on each refresh: the 7-day receipt window plus one.
REFRESH_DAYS = 8
#: Campaign messages are looked up this far before the refreshed days (a campaign's
#: messages expire within 7 days, so it never spans more).
CAMPAIGN_LOOKBACK = timedelta(days=7)
#: pg_try_advisory_xact_lock key: one refresh at a time across replicas.
LOCK_KEY = 0x4F414E53  # "OANS"
OPEN = ("accepted", "sending")
FINAL_FAILURES = ("failed", "expired", "unknown")
STATUS_COLUMNS = ("open", "sent", "delivered", "failed", "unknown", "expired")


def eat_day(ts: datetime) -> date:
    return ts.astimezone(EAT).date()


def day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=EAT)


_EDGES_SQL = "ARRAY[1,2,5,10,30,60,120,300,600,1800,3600,21600,86400]::double precision[]"
if _EDGES_SQL != "ARRAY[" + ",".join(map(str, BUCKET_EDGES_S)) + "]::double precision[]":
    raise RuntimeError("_EDGES_SQL must list BUCKET_EDGES_S")

_DAILY_SQL = """
SELECT (m.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS day,
       m.caller_id, m.channel, m.purpose, m.status,
       width_bucket(EXTRACT(EPOCH FROM (m.sent_at - m.created_at)), EDGES) AS send_bucket,
       width_bucket(EXTRACT(EPOCH FROM (m.delivered_at - m.created_at)), EDGES) AS deliver_bucket,
       count(*) AS n
FROM messages m
WHERE m.created_at >= :since
GROUP BY 1, 2, 3, 4, 5, 6, 7
""".replace("EDGES", _EDGES_SQL)

_FAILURES_SQL = """
SELECT (m.created_at AT TIME ZONE 'Africa/Addis_Ababa')::date AS day,
       m.caller_id, m.channel, m.purpose, m.failure_code, count(*) AS n
FROM messages m
WHERE m.created_at >= :since
  AND m.status IN ('failed', 'expired', 'unknown')
  AND m.failure_code IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
"""

_CAMPAIGNS_SQL = """
SELECT m.caller_id,
       split_part(m.source_reference, ':', 1) || ':' || split_part(m.source_reference, ':', 2)
           AS campaign,
       m.purpose,
       min(m.created_at) AS first_at,
       max(m.created_at) AS last_at,
       count(*) AS recipients,
       count(*) FILTER (WHERE m.status IN ('accepted', 'sending')) AS open,
       count(*) FILTER (WHERE m.status = 'sent') AS sent,
       count(*) FILTER (WHERE m.status = 'delivered') AS delivered,
       count(*) FILTER (WHERE m.status = 'failed') AS failed,
       count(*) FILTER (WHERE m.status = 'unknown') AS unknown,
       count(*) FILTER (WHERE m.status = 'expired') AS expired
FROM messages m
WHERE m.created_at >= :lookback
  AND m.purpose IN ('alert', 'broadcast')
  AND m.source_reference LIKE '%:%:%'
GROUP BY 1, 2, 3
HAVING count(*) >= 2 AND max(m.created_at) >= :since
"""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _status_column(status: str) -> str:
    return "open" if status in OPEN else status


async def refresh(sf: async_sessionmaker[AsyncSession], now: datetime) -> bool:
    """Rebuild the recent days (all days the first time). False if another refresh runs."""
    async with sf() as s, s.begin():
        locked = (
            await s.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        ).scalar_one()
        if not locked:
            return False
        first_time = (await s.execute(select(stats_state.c.as_of))).scalar_one_or_none() is None
        since_day = None if first_time else eat_day(now) - timedelta(days=REFRESH_DAYS)
        since = _EPOCH if since_day is None else day_start(since_day)

        daily: dict[tuple[date, str, str, str], dict[str, Any]] = {}
        for r in (await s.execute(text(_DAILY_SQL), {"since": since})).mappings():
            key = (r["day"], r["caller_id"], r["channel"], r["purpose"])
            row = daily.setdefault(
                key,
                {
                    "accepted": 0,
                    **dict.fromkeys(STATUS_COLUMNS, 0),
                    "send_hist": [0] * N_BUCKETS,
                    "deliver_hist": [0] * N_BUCKETS,
                },
            )
            n = int(r["n"])
            row["accepted"] += n
            column = _status_column(r["status"])
            if column in row:
                row[column] += n
            if r["send_bucket"] is not None:
                row["send_hist"][int(r["send_bucket"])] += n
            if r["deliver_bucket"] is not None:
                row["deliver_hist"][int(r["deliver_bucket"])] += n
        failures = [
            {
                "day": r["day"],
                "caller_id": r["caller_id"],
                "channel": r["channel"],
                "purpose": r["purpose"],
                "failure_code": r["failure_code"],
                "total": int(r["n"]),
            }
            for r in (await s.execute(text(_FAILURES_SQL), {"since": since})).mappings()
        ]

        if since_day is None:
            await s.execute(delete(stats_daily))
            await s.execute(delete(stats_failures))
        else:
            await s.execute(delete(stats_daily).where(stats_daily.c.day >= since_day))
            await s.execute(delete(stats_failures).where(stats_failures.c.day >= since_day))
        if daily:
            await s.execute(
                stats_daily.insert(),
                [
                    {"day": d, "caller_id": c, "channel": ch, "purpose": p, **v}
                    for (d, c, ch, p), v in daily.items()
                ],
            )
        if failures:
            await s.execute(stats_failures.insert(), failures)

        lookback = since if since_day is None else since - CAMPAIGN_LOOKBACK
        campaigns = [
            dict(r)
            for r in (
                await s.execute(text(_CAMPAIGNS_SQL), {"lookback": lookback, "since": since})
            ).mappings()
        ]
        if campaigns:
            stmt = pg_insert(stats_campaigns).values(campaigns)
            await s.execute(
                stmt.on_conflict_do_update(
                    constraint="pk_stats_campaigns",
                    set_={
                        c: stmt.excluded[c]
                        for c in ("first_at", "last_at", "recipients", *STATUS_COLUMNS)
                    },
                )
            )
        stmt = pg_insert(stats_state).values(id=1, as_of=now)
        await s.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={"as_of": now}))
    return True


async def refresh_forever(
    sf: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    *,
    interval: timedelta,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Refresh every ``interval``; a failed run is logged by type and retried next time."""
    while not stop.is_set():
        try:
            await refresh(sf, clock())
        except Exception as exc:
            log.error("statistics refresh failed error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            pass
