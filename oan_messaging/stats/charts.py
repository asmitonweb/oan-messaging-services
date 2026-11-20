"""Chart data for GET /api/v1/charts: the dashboard service contract of the OAN dashboards.

Like the other OAN dashboard services (A2C, Grievance, the registries), each chart is
``GET /api/v1/charts/<chartId>`` returning ``{"success": true, "data": [rows],
"meta": {"as_of": ...}}``, and ``GET /api/v1/charts?charts=a,b`` returns several at once
as ``data: {chartId: {"chartName": chartId, "data": [rows]}}``. Reports Central reads it
as an upstream.

Filters (all optional; other query parameters are ignored, as the dashboards pass their
own):

* ``from``, ``to``: Ethiopian calendar days ``YYYY-MM-DD`` (default: the last 30 days);
* ``granularity``: ``day`` (default), ``week`` (from Monday) or ``month``;
* ``caller``, ``purpose``, ``channel``: comma-separated lists.

Rows are counts only. ``open`` = waiting or being sent; ``sent`` = with the gateway, no
delivery report yet; the other columns are final statuses. Latency percentiles are read
from histograms and given as the upper edge of their bucket, in seconds (86400 = a day or
more).
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import Channel, Purpose
from oan_messaging.stats.snapshot import (
    BUCKET_EDGES_S,
    N_BUCKETS,
    STATUS_COLUMNS,
    day_start,
    eat_day,
)
from oan_messaging.storage.schema import (
    callers,
    dispatch_controls,
    messages,
    stats_campaigns,
    stats_daily,
    stats_failures,
    stats_state,
)

DEFAULT_DAYS = 30
MAX_DAYS = 366
CAMPAIGN_LIMIT = 200
_CALLER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COUNTS = ("accepted", *STATUS_COLUMNS)


class ChartQueryError(ValueError):
    """A filter is invalid. ``field`` and ``code`` are value-free."""

    def __init__(self, field: str, code: str):
        super().__init__(f"{field}: {code}")
        self.field, self.code = field, code


@dataclass(frozen=True)
class ChartQuery:
    start: date
    end: date
    granularity: str
    callers: tuple[str, ...]
    purposes: tuple[str, ...]
    channels: tuple[str, ...]


def _list(params: Mapping[str, str], name: str, check: Callable[[str], bool]) -> tuple[str, ...]:
    raw = params.get(name) or ""
    values = tuple(sorted({v.strip() for v in raw.split(",") if v.strip()}))
    if any(not check(v) for v in values):
        raise ChartQueryError(name, "invalid")
    return values


def parse_query(params: Mapping[str, str], today: date) -> ChartQuery:
    def day(name: str) -> date | None:
        raw = params.get(name)
        if raw is None or raw == "":
            return None
        if not _DATE_RE.fullmatch(raw):
            raise ChartQueryError(name, "invalid_format")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise ChartQueryError(name, "invalid_format") from None

    end = day("to") or today
    start = day("from") or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise ChartQueryError("from", "after_to")
    if (end - start).days + 1 > MAX_DAYS:
        raise ChartQueryError("from", "range_too_long")
    granularity = params.get("granularity") or "day"
    if granularity not in ("day", "week", "month"):
        raise ChartQueryError("granularity", "enum")
    purposes = {p.value for p in Purpose}
    channels = {c.value for c in Channel}
    return ChartQuery(
        start=start,
        end=end,
        granularity=granularity,
        callers=_list(params, "caller", lambda v: bool(_CALLER_RE.fullmatch(v))),
        purposes=_list(params, "purpose", lambda v: v in purposes),
        channels=_list(params, "channel", lambda v: v in channels),
    )


def _period(day: date, granularity: str) -> date:
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    return day


def percentile(hist: list[int], p: float) -> int | None:
    """Upper edge (seconds) of the bucket holding the p-th share; None when empty."""
    total = sum(hist)
    if not total:
        return None
    target = math.ceil(p * total)
    running = 0
    for i, n in enumerate(hist):
        running += n
        if running >= target:
            return BUCKET_EDGES_S[min(i, len(BUCKET_EDGES_S) - 1)]
    return BUCKET_EDGES_S[-1]


def _filters(table: Any, q: ChartQuery, *, purposes: tuple[str, ...] | None = None) -> list[Any]:
    out = [table.c.day >= q.start, table.c.day <= q.end]
    if q.callers:
        out.append(table.c.caller_id.in_(q.callers))
    wanted = q.purposes if purposes is None else purposes
    if wanted:
        out.append(table.c.purpose.in_(wanted))
    if q.channels:
        out.append(table.c.channel.in_(q.channels))
    return out


def _empty() -> dict[str, Any]:
    return {
        **dict.fromkeys(COUNTS, 0),
        "send_hist": [0] * N_BUCKETS,
        "deliver_hist": [0] * N_BUCKETS,
    }


def _add(acc: dict[str, Any], row: Mapping[str, Any]) -> None:
    for c in COUNTS:
        acc[c] += int(row[c])
    for h in ("send_hist", "deliver_hist"):
        acc[h] = [a + int(b) for a, b in zip(acc[h], row[h], strict=False)]


def _figures(acc: dict[str, Any], *, latency: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {c: acc[c] for c in COUNTS}
    accepted = acc["accepted"]
    out["delivered_share"] = round(acc["delivered"] / accepted, 4) if accepted else None
    out["failed_share"] = round(acc["failed"] / accepted, 4) if accepted else None
    if latency:
        out["send_p50_s"] = percentile(acc["send_hist"], 0.5)
        out["send_p95_s"] = percentile(acc["send_hist"], 0.95)
        out["deliver_p50_s"] = percentile(acc["deliver_hist"], 0.5)
        out["deliver_p95_s"] = percentile(acc["deliver_hist"], 0.95)
    return out


async def _daily(
    s: AsyncSession, q: ChartQuery, purposes: tuple[str, ...] | None = None
) -> list[Mapping[str, Any]]:
    rows = await s.execute(
        select(stats_daily).where(and_(*_filters(stats_daily, q, purposes=purposes)))
    )
    return [dict(r) for r in rows.mappings()]


def _grouped(
    rows: list[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], Any]
) -> dict[Any, dict[str, Any]]:
    groups: dict[Any, dict[str, Any]] = {}
    for r in rows:
        _add(groups.setdefault(key(r), _empty()), r)
    return groups


# ----------------------------------------------------------------------------- charts


async def chart_kpis(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    acc = _empty()
    for r in await _daily(s, q):
        _add(acc, r)
    return [_figures(acc)]


async def chart_trend(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    groups = _grouped(await _daily(s, q), lambda r: _period(r["day"], q.granularity))
    return [
        {"period": p.isoformat(), **_figures(acc, latency=False)}
        for p, acc in sorted(groups.items())
    ]


async def chart_by_caller(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    groups = _grouped(await _daily(s, q), lambda r: r["caller_id"])
    rows = [{"caller": c, **_figures(acc)} for c, acc in groups.items()]
    return sorted(rows, key=lambda r: (-r["accepted"], r["caller"]))


async def chart_by_purpose(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    order = {p.value: i for i, p in enumerate(Purpose)}
    groups = _grouped(await _daily(s, q), lambda r: r["purpose"])
    rows = [{"purpose": p, **_figures(acc)} for p, acc in groups.items()]
    return sorted(rows, key=lambda r: order.get(r["purpose"], 99))


async def chart_failure_reasons(
    s: AsyncSession, q: ChartQuery, now: datetime
) -> list[dict[str, Any]]:
    rows = await s.execute(
        select(stats_failures.c.failure_code, func.sum(stats_failures.c.total).label("total"))
        .where(and_(*_filters(stats_failures, q)))
        .group_by(stats_failures.c.failure_code)
    )
    data = [{"failure_code": r.failure_code, "total": int(r.total)} for r in rows]
    grand = sum(r["total"] for r in data)
    for r in data:
        r["share"] = round(r["total"] / grand, 4) if grand else None
    return sorted(data, key=lambda r: (-r["total"], r["failure_code"]))


async def chart_otp_latency(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    groups = _grouped(
        await _daily(s, q, purposes=("otp",)), lambda r: _period(r["day"], q.granularity)
    )
    return [
        {
            "period": p.isoformat(),
            "accepted": acc["accepted"],
            "sent_or_delivered": sum(acc["send_hist"]),
            "send_p50_s": percentile(acc["send_hist"], 0.5),
            "send_p95_s": percentile(acc["send_hist"], 0.95),
            "deliver_p50_s": percentile(acc["deliver_hist"], 0.5),
            "deliver_p95_s": percentile(acc["deliver_hist"], 0.95),
        }
        for p, acc in sorted(groups.items())
    ]


async def chart_campaigns(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    t = stats_campaigns
    where = [
        t.c.first_at >= day_start(q.start),
        t.c.first_at < day_start(q.end + timedelta(days=1)),
    ]
    if q.callers:
        where.append(t.c.caller_id.in_(q.callers))
    if q.purposes:
        where.append(t.c.purpose.in_(q.purposes))
    rows = await s.execute(
        select(t).where(and_(*where)).order_by(t.c.first_at.desc()).limit(CAMPAIGN_LIMIT)
    )
    out = []
    for r in rows.mappings():
        out.append(
            {
                "campaign": r["campaign"],
                "caller": r["caller_id"],
                "purpose": r["purpose"],
                "first_at": r["first_at"].isoformat(),
                "last_at": r["last_at"].isoformat(),
                "recipients": r["recipients"],
                **{c: r[c] for c in STATUS_COLUMNS},
                "delivered_share": round(r["delivered"] / r["recipients"], 4)
                if r["recipients"]
                else None,
            }
        )
    return out


async def chart_queue_now(s: AsyncSession, q: ChartQuery, now: datetime) -> list[dict[str, Any]]:
    """Live, not from the snapshot: what is waiting right now."""
    m = messages
    where = [m.c.status.in_(("accepted", "sending"))]
    if q.callers:
        where.append(m.c.caller_id.in_(q.callers))
    if q.purposes:
        where.append(m.c.purpose.in_(q.purposes))
    if q.channels:
        where.append(m.c.channel.in_(q.channels))
    accepted = m.c.status == "accepted"
    rows = await s.execute(
        select(
            m.c.purpose,
            m.c.channel,
            func.count().filter(accepted).label("waiting"),
            func.count()
            .filter(and_(accepted, (m.c.not_before.is_(None)) | (m.c.not_before <= now)))
            .label("due_now"),
            func.count().filter(m.c.status == "sending").label("sending"),
            func.min(m.c.created_at).filter(accepted).label("oldest"),
        )
        .where(and_(*where))
        .group_by(m.c.purpose, m.c.channel)
    )
    order = {p.value: i for i, p in enumerate(Purpose)}
    out = [
        {
            "purpose": r.purpose,
            "channel": r.channel,
            "waiting": int(r.waiting),
            "due_now": int(r.due_now),
            "sending": int(r.sending),
            "oldest_wait_s": int((now - r.oldest).total_seconds()) if r.oldest else None,
        }
        for r in rows
    ]
    return sorted(out, key=lambda r: (order.get(r["purpose"], 99), r["channel"]))


async def chart_provider_health(
    s: AsyncSession, q: ChartQuery, now: datetime
) -> list[dict[str, Any]]:
    """Live: each channel's dispatch control and its unknown outcomes in the last 24 hours."""
    chans = q.channels or tuple(c.value for c in Channel)
    controls = {
        r.channel: r
        for r in await s.execute(
            select(dispatch_controls).where(dispatch_controls.c.channel.in_(chans))
        )
    }
    where = [messages.c.created_at >= now - timedelta(hours=24), messages.c.status == "unknown"]
    if q.callers:
        where.append(messages.c.caller_id.in_(q.callers))
    unknown = {
        r.channel: int(r.n)
        for r in await s.execute(
            select(messages.c.channel, func.count().label("n"))
            .where(and_(*where))
            .group_by(messages.c.channel)
        )
    }
    out = []
    for c in sorted(chans):
        ctl = controls.get(c)
        cooldown = ctl.cooldown_until if ctl is not None else None
        out.append(
            {
                "channel": c,
                "suspended": bool(ctl.suspended) if ctl is not None else False,
                "cooling_down": bool(cooldown and cooldown > now),
                "cooldown_until": (
                    cooldown.isoformat() if cooldown and cooldown > now else None
                ),
                "unknown_last_24h": unknown.get(c, 0),
            }
        )
    return out


async def chart_filter_callers(
    s: AsyncSession, q: ChartQuery, now: datetime
) -> list[dict[str, Any]]:
    rows = await s.execute(
        select(callers.c.caller_id)
        .where(func.cardinality(callers.c.allowed_channels) > 0)
        .order_by(callers.c.caller_id)
    )
    return [{"caller": r.caller_id} for r in rows]


Builder = Callable[[AsyncSession, ChartQuery, datetime], Awaitable[list[dict[str, Any]]]]

CHARTS: dict[str, Builder] = {
    "msgKpis": chart_kpis,
    "msgTrend": chart_trend,
    "msgByCaller": chart_by_caller,
    "msgByPurpose": chart_by_purpose,
    "msgFailureReasons": chart_failure_reasons,
    "msgOtpLatency": chart_otp_latency,
    "msgCampaigns": chart_campaigns,
    "msgQueueNow": chart_queue_now,
    "msgProviderHealth": chart_provider_health,
    "msgFilterCallers": chart_filter_callers,
}
#: Read live from the messages table instead of the snapshot; ``as_of`` is the request time.
LIVE = frozenset({"msgQueueNow", "msgProviderHealth", "msgFilterCallers"})


class StatsService:
    def __init__(
        self,
        sf: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._sf, self._clock = sf, clock

    async def charts(self, chart_ids: list[str], params: Mapping[str, str]) -> dict[str, Any]:
        """``{id: rows}`` plus ``meta``; raises KeyError for an unknown chart and
        ChartQueryError for an invalid filter."""
        for cid in chart_ids:
            if cid not in CHARTS:
                raise KeyError(cid)
        now = self._clock()
        q = parse_query(params, eat_day(now))
        async with self._sf() as s:
            as_of = (await s.execute(select(stats_state.c.as_of))).scalar_one_or_none()
            data = {cid: await CHARTS[cid](s, q, now) for cid in chart_ids}
        live = all(cid in LIVE for cid in chart_ids)
        stamp = now if live else as_of
        meta = {
            "as_of": stamp.isoformat() if stamp is not None else None,
            "from": q.start.isoformat(),
            "to": q.end.isoformat(),
            "granularity": q.granularity,
        }
        return {"data": data, "meta": meta}


__all__ = ["CHARTS", "LIVE", "ChartQueryError", "StatsService", "parse_query", "percentile"]
