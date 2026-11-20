"""DA services: planned and rescheduled visit notifications.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION.

The DA service decides who is notified (the farmer, the assigned development agent, …)
and what each person is told. This example keeps the event's identity end to end:

* ``source_reference = da:visit:<visit_id>:<event_id>``, and
* one delivery per ``(event_id, recipient role)``.

A reschedule is a NEW event (new ``event_id``), so it is a new delivery. Replaying the
same event never sends twice.

Do NOT replace this with a generic "on document change, send SMS" hook (for example a
blanket Frappe ``doc_events`` handler). Such hooks lose the event identity, so retries
and repeated saves would look like new deliveries and send duplicates.

ASSUMED event schema (confirm with the DA services team):

===============  ===========================================================
event_id         unique id of this planned/rescheduled event (opaque)
visit_id         the visit's id
kind             "planned" | "rescheduled"
recipients       list of (role, phone, message_text); roles are opaque labels
visit_starts_at  visit time; notification deadline is capped below the API horizon
===============  ===========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from oan_client import OanClient, SmsDelivery, Submission, delivery_key


@dataclass(frozen=True, repr=False)
class VisitRecipient:
    role: str  # e.g. "farmer", "da-agent": chosen by the DA service
    phone: str
    message_text: str

    def __repr__(self) -> str:
        return f"VisitRecipient(role={self.role!r})"


@dataclass(frozen=True, repr=False)
class VisitEvent:
    event_id: str
    visit_id: str
    kind: Literal["planned", "rescheduled"]
    recipients: tuple[VisitRecipient, ...]
    visit_starts_at: datetime

    def __repr__(self) -> str:
        return f"VisitEvent(event_id={self.event_id!r}, kind={self.kind!r})"


def to_deliveries(event: VisitEvent, first_submitted_at: datetime, *,
                  max_expiry_horizon: timedelta = timedelta(days=7),
                  safety_margin: timedelta = timedelta(minutes=5)) -> list[SmsDelivery]:
    """Construct once at event creation; persist returned deliveries unchanged.

    The horizon is published by operators. Never recompute from a retry's current
    time: the fixed first_submitted_at anchors both key guarantee and deadline.
    """
    if not timedelta(0) < safety_margin < max_expiry_horizon:
        raise ValueError("safety margin must be positive and below the horizon")
    deadline = min(event.visit_starts_at,
                   first_submitted_at + max_expiry_horizon - safety_margin)
    if deadline <= first_submitted_at:
        raise ValueError("visit notification deadline has passed")
    roles = [r.role for r in event.recipients]
    if len(set(roles)) != len(roles):
        raise ValueError("each recipient role must be unique within an event")
    return [
        SmsDelivery(
            idempotency_key=delivery_key("da", event.event_id, r.role, "sms"),
            recipient=r.phone,
            text=r.message_text,
            source_reference=f"da:visit:{event.visit_id}:{event.event_id}",
            purpose="transactional",
            expires_at=deadline,
            first_submitted_at=first_submitted_at,
        )
        for r in event.recipients
    ]


async def notify_visit(client: OanClient, event: VisitEvent,
                       first_submitted_at: datetime) -> list[Submission]:
    return [await client.submit(d) for d in to_deliveries(event, first_submitted_at)]
