"""Grievance: acknowledgement and actionable-update SMS to farmers.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION.

The Grievance service alone owns farmer-facing grievance SMS. No other service (RapidPro
flows, DA tooling, generic hooks) should send these messages, or the farmer would get
duplicates from two senders that OAN cannot deduplicate across callers.

Each Grievance event that notifies someone becomes one delivery per recipient and
channel, one row in Grievance's own outbox (the existing *Grievance Notification Log*):

* ``Idempotency-Key = delivery_id``, a random UUID4 stored on that row when it is
  inserted and reused for every retry and sweep. It is **not** derived from the row name:
  ``GR-NOTIF-{######}`` restarts when a site is recreated, and a reused key inside OAN's
  30-day window would silently replay an old message instead of sending the new one.
* ``source_reference = grievance:<grievance_id>:<event>`` (shared by all deliveries of
  the event, not unique). Preserve event names exactly, including underscores such as
  ``deadline_extended``; contract 3.0.0 already permits them.

Dispatch, OAN message-id storage and status reconciliation follow
``examples/outbox_dispatch.py``: a 202/200 is recorded as *accepted* (Submitted), never as
Sent, and the OAN status is polled into a separate field.

ASSUMED event schema (confirm with the Grievance team):

================  ======================================================
delivery_id       UUID4 stored on the Notification Log row at insert
event             notification event name (e.g. ``acknowledged``)
grievance_id      the grievance's public reference (e.g. G-1042)
farmer_phone      farmer's number, selected by Grievance
message_text      rendered text, English or Amharic, one SMS part
================  ======================================================

Length: one SMS part only (pilot). That is 160 GSM-7 characters in English, but
**70 characters as soon as the text contains any Amharic (Ethiopic) character**, because
the whole message is then UCS-2 encoded. Longer text is rejected (HTTP 422), never split.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from oan_client import OanClient, SmsDelivery, Submission


@dataclass(frozen=True, repr=False)
class GrievanceEvent:
    delivery_id: str
    event: str
    grievance_id: str
    farmer_phone: str
    message_text: str

    def __repr__(self) -> str:
        return f"GrievanceEvent(grievance_id={self.grievance_id!r}, event={self.event!r})"


def to_delivery(event: GrievanceEvent, first_submitted_at: datetime) -> SmsDelivery:
    if uuid.UUID(event.delivery_id).version != 4:
        raise ValueError("delivery_id must be the UUID4 stored on the outbox row")
    return SmsDelivery(
        idempotency_key=event.delivery_id,
        recipient=event.farmer_phone,
        text=event.message_text,
        # The contract pattern allows [A-Za-z0-9._:-] only: Grievance event names use "_".
        source_reference=f"grievance:{event.grievance_id}:{event.event}",
        purpose="transactional",
        expires_at=None,  # no expiry policy assumed; Grievance may supply one
        first_submitted_at=first_submitted_at,
    )


async def notify(client: OanClient, event: GrievanceEvent,
                 first_submitted_at: datetime) -> Submission:
    return await client.submit(to_delivery(event, first_submitted_at))
