"""Operator controls, status checks and the worker liveness heartbeat (MSG-08).

Nothing here prints message content, recipients, chat IDs, keys or connection strings.
Status output contains only counts, ages in seconds and channel names.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import (
    Channel,
    FingerprintDomain,
    HmacFingerprinter,
    MessageStatus,
    ReceiptDisposition,
)
from oan_messaging.storage.crypto import ContentCipher
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import dispatch_controls, message_attempts, messages, receipts

DEFAULT_HEARTBEAT = Path("/tmp/oan-msg-worker.alive")  # noqa: S108 (per-pod emptyDir)


# ------------------------------------------------------------------ heartbeat


def touch_heartbeat(path: Path) -> None:
    """Record that the worker loop completed a cycle (success or handled failure)."""
    path.touch(exist_ok=True)
    os.utime(path)


def heartbeat_age(path: Path, *, now: float | None = None) -> float | None:
    """Seconds since the last heartbeat, or ``None`` if the worker never wrote one."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return max(0.0, (time.time() if now is None else now) - mtime)


# --------------------------------------------------------------- key check


async def missing_key_versions(
    repo: PostgresMessageRepository,
    cipher: ContentCipher,
    fingerprinter: HmacFingerprinter,
    now: datetime,
) -> list[str]:
    """Key versions still referenced by stored data but absent from this process's
    keyring, as ``content:<v>`` / ``fingerprint:<v>``. Version names only, never keys.

    A worker with a missing content key would defer those messages indefinitely; a
    missing fingerprint key breaks idempotency checks and receipt matching. Start-up
    refuses instead, so the misconfiguration fails the deployment visibly.
    """
    missing = [
        f"content:{v}"
        for v in sorted(await repo.content_key_versions_in_use())
        if v not in cipher.versions()
    ]
    for v in sorted(await repo.fingerprint_key_versions_in_use(now)):
        try:
            fingerprinter.fingerprint(FingerprintDomain.PAYLOAD, b"key-check", key_version=v)
        except ValueError:
            missing.append(f"fingerprint:{v}")
    return missing


# ------------------------------------------------------------- scope controls


async def set_scope(
    sf: async_sessionmaker[AsyncSession], channel: Channel, *, suspended: bool, now: datetime
) -> None:
    """Suspend or resume dispatch for one provider channel.

    Upsert: a suspend must take effect even before any send has created the channel's
    ``dispatch_controls`` row. Resume also clears the cooldown and the ambiguous counter.
    Statuses, expiry and stored messages are never changed here.
    """
    values: dict[str, Any] = {
        "suspended": suspended,
        "cooldown_until": None,
        "ambiguous_count": 0,
        "updated_at": now,
    }
    async with sf() as s, s.begin():
        await s.execute(
            pg_insert(dispatch_controls)
            .values(channel=channel.value, **values)
            .on_conflict_do_update(index_elements=[dispatch_controls.c.channel], set_=values)
        )


# --------------------------------------------------------------------- status


@dataclass(frozen=True)
class Thresholds:
    """Provisional alert thresholds; operators tune them per environment (runbook)."""

    oldest_due_accepted: timedelta = timedelta(minutes=5)
    stale_sending: timedelta = timedelta(minutes=5)
    unknown_recent: int = 1
    late_starts_recent: int = 1
    deferred_due: int = 1
    receipt_anomalies_recent: int = 1


async def ops_status(
    sf: async_sessionmaker[AsyncSession],
    now: datetime,
    thresholds: Thresholds = Thresholds(),  # noqa: B008 (immutable)
) -> dict[str, Any]:
    """Counts and ages for alerting. ``alerts`` lists the names of breached checks."""
    hour = now - timedelta(hours=1)
    day = now - timedelta(days=1)
    async with sf() as s:
        by_status = dict(
            (
                await s.execute(
                    select(messages.c.status, func.count())
                    .where(
                        messages.c.status.in_(
                            [
                                MessageStatus.ACCEPTED.value,
                                MessageStatus.SENDING.value,
                                MessageStatus.UNKNOWN.value,
                            ]
                        )
                    )
                    .group_by(messages.c.status)
                )
            )
            .tuples()
            .all()
        )
        due = and_(
            messages.c.status == MessageStatus.ACCEPTED.value,
            or_(messages.c.not_before.is_(None), messages.c.not_before <= now),
            or_(messages.c.expires_at.is_(None), messages.c.expires_at > now),
        )
        oldest_due = (await s.execute(select(func.min(messages.c.created_at)).where(due))).scalar()
        # Accepted, not yet due and never attempted: deferred dispatch, which includes rows
        # whose content cannot currently be decrypted (F-01 deferral).
        deferred = (
            await s.execute(
                select(func.count()).where(
                    messages.c.status == MessageStatus.ACCEPTED.value,
                    messages.c.not_before > now,
                    messages.c.attempts == 0,
                )
            )
        ).scalar_one()
        stale_sending = (
            await s.execute(
                select(func.count()).where(
                    messages.c.status == MessageStatus.SENDING.value,
                    messages.c.lease_expires_at < now - thresholds.stale_sending,
                )
            )
        ).scalar_one()
        unknown_recent = (
            await s.execute(
                select(func.count()).where(
                    messages.c.status == MessageStatus.UNKNOWN.value, messages.c.updated_at > day
                )
            )
        ).scalar_one()
        late_recent = (
            await s.execute(
                select(func.count()).where(
                    message_attempts.c.provider_status == "local_start_late",
                    message_attempts.c.started_at > hour,
                )
            )
        ).scalar_one()
        anomalies = dict(
            (
                await s.execute(
                    select(receipts.c.disposition, func.count())
                    .where(
                        receipts.c.received_at > day,
                        receipts.c.disposition.in_(
                            [
                                ReceiptDisposition.ORPHANED.value,
                                ReceiptDisposition.MISMATCHED.value,
                                ReceiptDisposition.UNMATCHED_LATE.value,
                            ]
                        ),
                    )
                    .group_by(receipts.c.disposition)
                )
            )
            .tuples()
            .all()
        )
        scopes = (
            (
                await s.execute(
                    select(
                        dispatch_controls.c.channel,
                        dispatch_controls.c.suspended,
                        dispatch_controls.c.cooldown_until,
                    ).order_by(dispatch_controls.c.channel)
                )
            )
            .mappings()
            .all()
        )
    oldest_due_seconds = None if oldest_due is None else (now - oldest_due).total_seconds()
    scope_view = {
        str(r["channel"]): {
            "suspended": bool(r["suspended"]),
            "cooldown_seconds": max(0.0, (r["cooldown_until"] - now).total_seconds())
            if r["cooldown_until"] is not None
            else 0.0,
        }
        for r in scopes
    }
    alerts: list[str] = []
    if (
        oldest_due_seconds is not None
        and oldest_due_seconds > thresholds.oldest_due_accepted.total_seconds()
    ):
        alerts.append("queue_age")
    if stale_sending:
        alerts.append("stale_sending")
    if unknown_recent >= thresholds.unknown_recent:
        alerts.append("unknown_outcomes")
    if late_recent >= thresholds.late_starts_recent:
        alerts.append("late_starts")
    if deferred >= thresholds.deferred_due:
        alerts.append("deferred_dispatch")
    if sum(anomalies.values()) >= thresholds.receipt_anomalies_recent:
        alerts.append("receipt_anomalies")
    alerts += [f"suspended:{ch}" for ch, v in scope_view.items() if v["suspended"]]
    return {
        "at": now.isoformat(),
        "messages": {
            "accepted": int(by_status.get(MessageStatus.ACCEPTED.value, 0)),
            "sending": int(by_status.get(MessageStatus.SENDING.value, 0)),
            "unknown": int(by_status.get(MessageStatus.UNKNOWN.value, 0)),
        },
        "oldest_due_accepted_seconds": oldest_due_seconds,
        "deferred_not_yet_attempted": int(deferred),
        "stale_sending": int(stale_sending),
        "unknown_last_24h": int(unknown_recent),
        "late_starts_last_hour": int(late_recent),
        "receipt_anomalies_last_24h": {k: int(v) for k, v in anomalies.items()},
        "provider_scopes": scope_view,
        "alerts": alerts,
    }
