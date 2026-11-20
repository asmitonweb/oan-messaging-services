"""Owning-service outbox: durable delivery identity, post-commit dispatch, OAN message-ID
storage and status reconciliation.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION. It is framework-neutral;
``OutboxRow`` stands for a row in the owning service's own table (for Grievance on Frappe,
the existing *Grievance Notification Log*). No upstream repository is changed.

The flow the owning service implements in its own repository:

1. **Business transaction.** Insert one outbox row per intended delivery (per recipient
   and channel): ``delivery_id`` = a random UUID4 fixed at insert (the
   ``Idempotency-Key``), the rendered text, recipient, ``purpose``, ``source_reference``,
   ``expires_at`` and ``first_submitted_at`` (the insert time, a conservative start of
   the guarantee window). Nothing is sent inside the transaction.
2. **Post-commit job.** Enqueue ``dispatch`` only after commit (Frappe
   ``enqueue_after_commit=True``), plus a periodic recovery sweep for ``queued`` rows
   whose ``next_attempt_at`` has passed. Lock each row (``SELECT … FOR UPDATE SKIP
   LOCKED``), rebuild the identical delivery from the stored fields, call OAN, record the
   outcome and commit per row.
3. **Acceptance is not delivery.** OAN ``202``/``200`` means *accepted*: store the OAN
   message id and move the row to ``accepted``. Do not record ``Sent`` or ``Delivered``.
4. **Reconciliation.** Poll ``GET /api/v1/messages/{id}`` for accepted rows until OAN
   reports final or the caller polling policy completes; store the OAN status separately
   (``oan_status``). OAN has no status
   push.

Never: derive the key from a sequence that can restart (row names such as
``GR-NOTIF-{######}`` restart when a site is recreated, and a reused key inside OAN's
30-day window silently replays an old message), mark an uncertain outcome as failed and
re-insert it under a new key, or resubmit a row whose OAN status is ``unknown``.

Row states (Grievance today has Queued / Sent / Failed):

===========  ===============================================  ==========================
state        meaning                                          Grievance status
===========  ===============================================  ==========================
queued       not yet accepted by OAN (new or uncertain)       Queued
accepted     OAN stored it; ``oan_message_id`` set             Submitted (new value)
rejected     definite 4xx; ``error_code`` set; alert          Failed
conflict     409: one key, two bodies (a bug); alert          Failed
reconcile    guarantee window passed; operators reconcile     Failed + operator alert
===========  ===============================================  ==========================

The delivery outcome lives in ``oan_status`` (a separate field, e.g. ``delivery_status``):
``accepted``, ``sending``, ``sent``, ``delivered``, ``failed``, ``expired`` or ``unknown``.

Scheduling is persisted on the row, in two separate fields, and checked against the
caller-supplied ``now`` before any request:

* ``next_attempt_at`` gates **submission** retries (``queued`` rows only);
* ``next_poll_at`` gates **status polls** (``accepted`` rows only).

Both are set no earlier than the server's ``retry_not_before`` (``Retry-After``, seconds
or HTTP-date), whether the client deferred or exhausted its attempts, and otherwise by a
bounded exponential backoff. Polling is GET-only: no polling outcome ever moves a row back
to ``queued``, so an accepted or ``unknown`` message is never submitted again. A malformed
response or unexpected error on one row is recorded on that row (``error_code`` plus
``needs_attention`` for operators) and scheduled for later; the sweep goes on to the next
row.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from oan_client import (
    OanClient,
    OanClientError,
    OanConflict,
    OanProtocolError,
    OanRejected,
    OanUncertain,
    ReconciliationRequired,
    SmsDelivery,
)

#: Delay before the recovery sweep retries an uncertain submission (never earlier than
#: the server's ``Retry-After``).
DEFAULT_RETRY_DELAY = timedelta(minutes=5)
#: Status polling backoff: first poll interval, doubling per poll, capped.
POLL_BASE = timedelta(minutes=1)
POLL_CAP = timedelta(hours=6)

_DATETIME_FIELDS = (
    "expires_at", "first_submitted_at", "next_attempt_at", "next_poll_at", "poll_deadline"
)


@dataclass(repr=False)
class OutboxRow:
    delivery_id: str  #: UUID4 fixed at insert; the Idempotency-Key
    recipient: str
    text: str
    source_reference: str
    purpose: str
    expires_at: datetime | None
    first_submitted_at: datetime  #: insert time; starts the guarantee window
    state: str = "queued"
    attempts: int = 0  #: dispatch runs (each may make several HTTP attempts)
    next_attempt_at: datetime | None = None  #: submission retry gate (queued rows)
    oan_message_id: str | None = None
    oan_status: str | None = None
    oan_final: bool = False
    polls: int = 0  #: status polls made (drives the polling backoff)
    next_poll_at: datetime | None = None  #: status poll gate (accepted rows)
    error_code: str | None = None  #: last safe error code (contract values only)
    needs_attention: bool = False  #: operator signal
    poll_complete: bool = False  #: caller lifecycle; does NOT change oan_final
    poll_deadline: datetime | None = None  #: fixed after first observed SMS sent


    def to_delivery(self) -> SmsDelivery:
        """Rebuilt from stored fields, so every run sends byte-identical requests."""
        return SmsDelivery(idempotency_key=self.delivery_id, recipient=self.recipient,
                           text=self.text, source_reference=self.source_reference,
                           purpose=self.purpose, expires_at=self.expires_at,
                           first_submitted_at=self.first_submitted_at)

    def to_record(self) -> dict[str, Any]:
        """What the owning service persists (datetimes as ISO 8601 with offset)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.isoformat() if isinstance(value, datetime) else value
        return out

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> OutboxRow:
        """Reload a persisted row (e.g. after a restart). Schedules survive unchanged."""
        data = dict(record)
        for name in _DATETIME_FIELDS:
            if data.get(name) is not None:
                data[name] = datetime.fromisoformat(data[name])
        return cls(**data)

    def __repr__(self) -> str:  # never the text or the recipient
        return (f"OutboxRow(key=…{self.delivery_id[-4:]}, state={self.state!r}, "
                f"oan_status={self.oan_status!r})")


def _backoff(count: int, base: timedelta, cap: timedelta) -> timedelta:
    return min(cap, base * (1 << min(max(count - 1, 0), 20)))


def _not_before(fallback: datetime, server: datetime | None) -> datetime:
    """The later of our own schedule and the server's ``retry_not_before``."""
    return server if server is not None and server > fallback else fallback


def is_due(row: OutboxRow, now: datetime) -> bool:
    """Due for a submission attempt."""
    return row.state == "queued" and (row.next_attempt_at is None or now >= row.next_attempt_at)


def is_poll_due(row: OutboxRow, now: datetime) -> bool:
    """Due for a status poll (GET only)."""
    return (row.state == "accepted" and not row.oan_final and not row.poll_complete
            and row.oan_message_id is not None
            and (row.next_poll_at is None or now >= row.next_poll_at))


async def dispatch(client: OanClient, row: OutboxRow, now: datetime, *,
                   retry_delay: timedelta = DEFAULT_RETRY_DELAY) -> None:
    """One post-commit (or sweep) run for one locked row. Updates the row in place; the
    caller commits it."""
    if not is_due(row, now):
        return
    # Only an untouched row is provably never submitted. Uncertain previous
    # attempts must still replay the identical key, even after its expiry.
    if row.attempts == 0 and row.expires_at is not None and row.expires_at <= now:
        row.state, row.error_code = "rejected", "expired_before_submission"
        row.needs_attention = True
        row.next_attempt_at = None
        return
    row.attempts += 1
    try:
        sub = await client.submit(row.to_delivery())
    except ReconciliationRequired:
        row.state, row.next_attempt_at, row.needs_attention = "reconcile", None, True
        return
    except OanConflict as exc:
        row.state, row.error_code, row.next_attempt_at = "conflict", exc.code, None
        row.needs_attention = True
        return
    except OanRejected as exc:
        row.state = "rejected"
        row.error_code = f"{exc.status}:{exc.code}"
        row.next_attempt_at = None
        row.needs_attention = True
        return
    except OanUncertain as exc:  # includes OanRetryDeferred
        row.next_attempt_at = _not_before(now + retry_delay, exc.retry_not_before)
        return
    except OanProtocolError:
        # Unexpected response shape or redirect: the message may be stored. A later
        # same-key retry inside the guarantee window is safe (a replay); never a new key.
        # The client raises ReconciliationRequired once the window has passed.
        row.next_attempt_at = now + retry_delay
        row.error_code, row.needs_attention = "protocol_error", True
        return
    row.state = "accepted"  # accepted by OAN; NOT sent and NOT delivered
    row.oan_message_id = sub.id
    row.oan_status = sub.status
    row.next_attempt_at = None
    row.next_poll_at = now  # first status poll may follow immediately
    row.error_code = None


async def reconcile(client: OanClient, row: OutboxRow, now: datetime, *,
                    poll_base: timedelta = POLL_BASE, poll_cap: timedelta = POLL_CAP,
                    receipt_wait: timedelta = timedelta(days=7)) -> None:
    """Refresh ``oan_status`` for an accepted row with one ``GET``. Never submits, and
    never moves the row back to ``queued``. Does nothing before ``next_poll_at``."""
    if receipt_wait <= timedelta(0):
        raise ValueError("receipt_wait must be positive")
    if not is_poll_due(row, now):
        return
    if row.poll_deadline is not None and now >= row.poll_deadline:
        row.poll_complete, row.next_poll_at = True, None
        row.error_code, row.needs_attention = "receipt_wait_elapsed", True
        return
    assert row.oan_message_id is not None  # guaranteed by is_poll_due
    row.polls += 1
    fallback = now + _backoff(row.polls, poll_base, poll_cap)
    try:
        st = await client.get_status(row.oan_message_id)
    except OanUncertain as exc:  # includes OanRetryDeferred: keep the server's deadline
        row.next_poll_at = _not_before(fallback, exc.retry_not_before)
        return
    except OanRejected as exc:
        row.error_code, row.needs_attention = f"{exc.status}:{exc.code}", True
        if exc.status == 404:  # id unknown to this caller: operators look it up
            row.state, row.next_poll_at = "reconcile", None
        else:  # e.g. 401/403 after a credential change: keep polling, slowly
            row.next_poll_at = fallback
        return
    except (OanClientError, ValueError):  # includes OanProtocolError
        # Malformed response, redirect or a bad stored id: record it on this row only.
        row.error_code, row.needs_attention = "status_protocol_error", True
        row.next_poll_at = fallback
        return
    row.oan_status, row.oan_final = st.status, st.final
    row.error_code = None
    row.poll_complete = st.final or st.status == "unknown" or (
        st.channel == "telegram" and st.status == "sent"
    )
    if st.status == "unknown":
        row.needs_attention, row.error_code = True, "unknown_outcome"
    elif st.channel == "sms" and st.status == "sent":
        if row.poll_deadline is None:
            # Independent caller policy, not OAN's receipt-matching retention.
            # Persist first observation fallback if a sent timestamp is absent.
            raw_sent = st.raw.get("sent_at")
            anchor = (datetime.fromisoformat(raw_sent.replace("Z", "+00:00"))
                      if isinstance(raw_sent, str) else now)
            if anchor.tzinfo is None or anchor > now:
                anchor = now
            row.poll_deadline = anchor + receipt_wait
        if now >= row.poll_deadline:
            row.poll_complete = True
            row.needs_attention, row.error_code = True, "receipt_wait_elapsed"
        fallback = min(fallback, row.poll_deadline)
    row.next_poll_at = None if row.poll_complete else fallback


async def poll_sweep(client: OanClient, rows: Iterable[OutboxRow], now: datetime) -> int:
    """Poll every due row once. Each row's failure stays on that row. Returns the number
    of rows polled."""
    polled = 0
    for row in rows:
        if is_poll_due(row, now):
            await reconcile(client, row, now)
            polled += 1
    return polled
