"""Credit (A2C, Access to Credit): farmer SMS for the loan lifecycle.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION. Built the same way as
``grievance_notifications.py``; read that file and ``outbox_dispatch.py`` first.

Grounded in ``oan_a2c_service`` at develop 3887b8b (read-only review, 15 Nov 2026).
That commit sends the farmer **no SMS at all**: every notification is an in-app
Notification Log for bank staff or the assigned agent. The consent OTP is sent by the
external Consent Manager, not by A2C, and is out of scope here (see
``dpi_consent_notifications.py``).

Event catalogue (``A2C_EVENTS``), with the A2C call site that should raise each event:

======================  ===================================================================
consent_approved        ``api/v1/consent/consent.py`` ``submit_consent`` sets Approved
consent_failed          ``api/v1/webhook_consent_data.py`` sets Failed
application_submitted   ``api/v1/farmer/applications.py`` ``submit_application``
stage_changed           ``api/v1/loan_applications.py`` ``update_loan_status`` (In Transition)
application_approved    same, when the bank's stage label is "Approved" (bank-defined)
application_rejected    same, Reject transition (status Rejected)
loan_disbursed          same, Complete transition (status Completed, default label
                        "Disbursed")
visit_scheduled         ``api/v1/leads.py`` ``schedule_visit``
visit_cancelled         ``api/v1/leads.py`` ``update_visit_schedule_status`` -> Cancelled
======================  ===================================================================

Raise events from those explicit call sites (or from ``api/utils.py``
``apply_status_transition``), **not** from ``on_update``: A2C mirrors ``status`` with
``db_set`` after the workflow save, which skips ``on_update``.

Not offered, because A2C has no data for them at 3887b8b: documents requested,
repayment due or overdue, disbursal confirmation from a bank integration.

Delivery rules (same as Grievance):

* ``Idempotency-Key = delivery_id``: a random UUID4 stored on the A2C outbox row when it
  is inserted (a new *A2C Notification Log* doctype shaped like *Grievance Notification
  Log*), reused for every retry. Never derived from ``APP-{YYYY}-{#####}``.
* ``source_reference = a2c:<record>:<event>``, where record is the application
  (``APP-...``) or visit (``VSCH-...``) name.
* Purpose ``transactional``. Visit messages expire at the visit start, capped below the
  API horizon exactly like ``da_visits.py``; nothing else expires.
* One SMS part only: 160 GSM-7 characters, or 70 if the text contains any Amharic
  character. ``render`` refuses text that does not fit, so a long bank or place name
  fails at the A2C side, not as an HTTP 422.
* Messages never include amounts, rejection reasons or other free text typed by bank
  staff: they are short, safe pointers back to the agent or bank.

Templates are English only. Amharic, Afaan Oromo and other translations are owned by the
A2C team, added to ``TEMPLATES`` and checked by the same length rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from oan_client import OanClient, SmsDelivery, Submission
from rapidpro_mapping import fits_one_sms

#: English source templates. Placeholders: bank, app, stage, visit, date, time, place.
TEMPLATES: dict[str, dict[str, str]] = {
    "consent_approved": {
        "en": "OAN Credit: you agreed to share your data with {bank} for loan application "
        "{app}. You can withdraw this consent at any time.",
    },
    "consent_failed": {
        "en": "OAN Credit: we could not record your consent for {bank}. Please try again "
        "or ask your agent for help.",
    },
    "application_submitted": {
        "en": "OAN Credit: your loan application {app} was sent to {bank}. We will tell "
        "you when it moves forward.",
    },
    "stage_changed": {
        "en": "OAN Credit: your loan application {app} with {bank} is now at stage: {stage}.",
    },
    "application_approved": {
        "en": "OAN Credit: {bank} approved your loan application {app}. The bank will "
        "contact you about next steps.",
    },
    "application_rejected": {
        "en": "OAN Credit: {bank} did not approve loan application {app}. Ask your agent "
        "or the bank for the reason.",
    },
    "loan_disbursed": {
        "en": "OAN Credit: {bank} has paid out the loan for application {app}. Contact the "
        "bank if you have not received it.",
    },
    "visit_scheduled": {
        "en": "OAN Credit: an agent will visit you on {date} at {time}, at {place}. "
        "Visit ref {visit}.",
    },
    "visit_cancelled": {
        "en": "OAN Credit: agent visit {visit} on {date} is cancelled. Your agent will "
        "contact you to book a new time.",
    },
}

A2C_EVENTS = tuple(TEMPLATES)
VISIT_EVENTS = frozenset({"visit_scheduled", "visit_cancelled"})

#: Longest values A2C may substitute; ``tests`` render every template at these lengths.
FIELD_LIMITS = {"bank": 30, "app": 14, "stage": 30, "visit": 15, "date": 11, "time": 5, "place": 40}


class TemplateTooLong(ValueError):
    """The rendered text does not fit one SMS part. Shorten the template or the value."""


def render(event: str, language: str = "en", **fields: str) -> str:
    """Render ``event`` in ``language`` (falls back to English when no translation)."""
    if event not in TEMPLATES:
        raise ValueError(f"unknown A2C event {event!r}")
    for name, value in fields.items():
        if name in FIELD_LIMITS and len(value) > FIELD_LIMITS[name]:
            raise TemplateTooLong(f"{name} longer than {FIELD_LIMITS[name]} characters")
    template = TEMPLATES[event].get(language) or TEMPLATES[event]["en"]
    text = template.format(**fields)
    if not fits_one_sms(text):
        raise TemplateTooLong(f"{event}/{language} does not fit one SMS part")
    return text


def event_for_transition(new_status: str, stage_label: str | None) -> str | None:
    """Map an A2C status or stage change to a farmer event (``None``: do not notify).

    ``new_status`` is the application ``status`` after ``apply_status_transition``;
    ``stage_label`` is the bank-defined ``stage_label``. Banks name their own stages, so
    "approved" is matched by label: each bank should confirm its label.
    """
    if new_status == "Rejected":
        return "application_rejected"
    if new_status == "Completed":
        return "loan_disbursed"
    if new_status == "In Transition" and stage_label:
        return (
            "application_approved" if stage_label.strip().lower() == "approved" else "stage_changed"
        )
    return None


@dataclass(frozen=True, repr=False)
class A2CNotification:
    delivery_id: str
    event: str
    record_id: str  # APP-YYYY-##### or VSCH-YYYY-#####
    farmer_phone: str
    message_text: str
    #: Visit events only: when the visit starts (the message is useless afterwards).
    visit_starts_at: datetime | None = None

    def __repr__(self) -> str:  # never the phone or the text
        return f"A2CNotification(record_id={self.record_id!r}, event={self.event!r})"


def to_delivery(
    note: A2CNotification,
    first_submitted_at: datetime,
    *,
    max_expiry_horizon: timedelta = timedelta(days=7),
    safety_margin: timedelta = timedelta(minutes=5),
) -> SmsDelivery:
    """Build once when the outbox row is inserted; retries resend the identical body."""
    if uuid.UUID(note.delivery_id).version != 4:
        raise ValueError("delivery_id must be the UUID4 stored on the outbox row")
    if note.event not in A2C_EVENTS:
        raise ValueError(f"unknown A2C event {note.event!r}")
    expires_at = None
    if note.event in VISIT_EVENTS:
        if note.visit_starts_at is None:
            raise ValueError("visit events need visit_starts_at")
        expires_at = min(
            note.visit_starts_at, first_submitted_at + max_expiry_horizon - safety_margin
        )
        if expires_at <= first_submitted_at:
            raise ValueError("visit notification deadline has passed")
    return SmsDelivery(
        idempotency_key=note.delivery_id,
        recipient=note.farmer_phone,
        text=note.message_text,
        source_reference=f"a2c:{note.record_id}:{note.event}",
        purpose="transactional",
        expires_at=expires_at,
        first_submitted_at=first_submitted_at,
    )


async def notify(
    client: OanClient, note: A2CNotification, first_submitted_at: datetime
) -> Submission:
    return await client.submit(to_delivery(note, first_submitted_at))
