"""DPI Consent Manager: deliver consent OTPs and consent notices by SMS.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION.

Grounded in ``consent-management`` (OpenG2P Consent Manager) at main b6d5e45, reviewed
read-only on 15 November 2026. Unlike Grievance and A2C, it is **not a Frappe app**. It is
an async FastAPI service, so it can call ``OanClient`` directly; no ``asyncio.run``
wrapper is needed.

**The gap this closes.** At b6d5e45, the consent OTP is generated, hashed and bound to
the transaction, but it is **never delivered**. ``services/otp_provider.py`` only logs
"NO message was delivered". ``OtpService`` (``services/otp_service.py``) is the plug
point. Every consent-gated data release (A2C, bank partners) is blocked on this
delivery.

OTP messages (``OtpMessage``):

* **consent**: ``lifecycle_service.issue_otp``. The farmer approves sharing with a
  partner.
* **data_release**: ``aggregator_service`` sets ``pending_otp``. The farmer releases
  registry data to a partner.
* Purpose ``otp``, and ``expires_at`` is the OTP's own expiry, passed through unchanged
  (Consent Manager uses 300 s). OAN never extends it and drops the message if it cannot
  send it in time.
* Key: ``delivery_key("dpi-consent", <transactionID>, "otp-<kind>-<issue_no>", "sms")``.
  ``issue_no`` increases on every resend, so a resend is a new delivery with a new code.
  Retries of the **same** OTP reuse the key, so the farmer never gets the code twice.
  Deterministic keys are safe because transaction IDs are unique and never reused.

Notices (``ConsentNotice``, purpose ``transactional``, no expiry):

=================  ==========================================================
consent_granted    ``lifecycle_service`` approve (artefact created)
consent_denied     ``lifecycle_service`` deny
consent_revoked    ``consent_service`` revoke (by subject, controller or partner)
consent_expired    ``consent_service`` expiry cron
data_shared        ``aggregator_service`` aggregation delivered to the partner
=================  ==========================================================

Not offered: "consent expiring soon" (it needs a new cron on ``valid_until``) and
"request expired" (Consent Manager never sets that status at b6d5e45).

**Recipient: the DPI team must decide which phone is authoritative.** Consent Manager
stores no phone. It identifies the farmer only by Fayda FIN, so ``PhoneResolver`` must
look the number up, for example as registry ``demographic_info.phone_number``. For OTPs,
the phone must be one the farmer controls and the identity owner trusts. That is an
identity decision, not a transport one. If no phone is found, the OTP is not sent and the
consent flow must fail visibly.

Text: one SMS part (160 GSM-7, or 70 with any Amharic). The OTP code never appears in
logs, reprs or source references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from oan_client import OanClient, SmsDelivery, Submission, delivery_key
from rapidpro_mapping import fits_one_sms

OTP_TEMPLATES = {
    "consent": "OAN: {code} is your code to allow {partner} to use your registry data. "
    "It expires in {minutes} minutes. Never share this code.",
    "data_release": "OAN: {code} is your code to release your registry data to {partner}. "
    "It expires in {minutes} minutes. Never share this code.",
}
NOTICE_TEMPLATES = {
    "consent_granted": "OAN: you allowed {partner} to use your registry data until {until}. "
    "Ref {ref}. You can withdraw this at any time.",
    "consent_denied": "OAN: you refused {partner} access to your registry data. Ref {ref}.",
    "consent_revoked": "OAN: consent for {partner} to use your registry data was withdrawn "
    "by {who}. Ref {ref}.",
    "consent_expired": "OAN: consent for {partner} to use your registry data has ended. Ref {ref}.",
    "data_shared": "OAN: your registry data was sent to {partner} under your consent. "
    "Ref {ref}. Contact OAN if you did not agree to this.",
}
FIELD_LIMITS = {"partner": 30, "until": 11, "ref": 8, "who": 7}
_CODE = re.compile(r"^[0-9]{4,8}$")


class TemplateTooLong(ValueError):
    pass


def _fit(text: str, what: str) -> str:
    if not fits_one_sms(text):
        raise TemplateTooLong(f"{what} does not fit one SMS part")
    return text


def short_ref(record_id: str) -> str:
    """Last 8 characters of a Consent Manager id, upper-case, for support calls."""
    cleaned = record_id.replace("-", "")
    if len(cleaned) < 8:
        raise ValueError("record id too short for a reference")
    return cleaned[-8:].upper()


class PhoneResolver(Protocol):
    """Resolve a consent subject (Fayda FIN by default) to a deliverable phone number."""

    async def phone_for(self, subject_id_type: str, subject_id_value: str) -> str | None: ...


@dataclass(frozen=True, repr=False)
class OtpMessage:
    kind: str  # "consent" or "data_release"
    transaction_id: str  # Consent Manager correlation_id (the OTP transactionID)
    issue_no: int  # 1 for the first OTP, +1 for every resend
    code: str
    partner: str
    expires_at: datetime
    phone: str

    def __repr__(self) -> str:  # never the code or the phone
        return f"OtpMessage(kind={self.kind!r}, transaction_id={self.transaction_id!r})"


def otp_delivery(msg: OtpMessage, issued_at: datetime) -> SmsDelivery:
    if msg.kind not in OTP_TEMPLATES:
        raise ValueError(f"unknown OTP kind {msg.kind!r}")
    if not _CODE.fullmatch(msg.code):
        raise ValueError("OTP code must be 4-8 digits")
    if msg.issue_no < 1:
        raise ValueError("issue_no starts at 1")
    if len(msg.partner) > FIELD_LIMITS["partner"]:
        raise TemplateTooLong("partner name longer than 30 characters")
    if msg.expires_at <= issued_at:
        raise ValueError("OTP already expired")
    minutes = max(1, round((msg.expires_at - issued_at).total_seconds() / 60))
    text = _fit(
        OTP_TEMPLATES[msg.kind].format(code=msg.code, partner=msg.partner, minutes=minutes),
        f"otp/{msg.kind}",
    )
    return SmsDelivery(
        idempotency_key=delivery_key(
            "dpi-consent", msg.transaction_id, f"otp-{msg.kind}-{msg.issue_no}", "sms"
        ),
        recipient=msg.phone,
        text=text,
        source_reference=f"dpi-consent:{msg.transaction_id}:otp-{msg.kind}",
        purpose="otp",
        expires_at=msg.expires_at,  # the OTP's own deadline, never extended
        first_submitted_at=issued_at,
    )


@dataclass(frozen=True, repr=False)
class ConsentNotice:
    event: str
    record_id: str  # ConsentArtefact.id / ConsentRequest.id / AggregationRequest.id
    partner: str
    phone: str
    until: str | None = None  # consent_granted: valid_until as "3 Sep 2027"
    who: str | None = None  # consent_revoked: "you", "OAN" or "partner"

    def __repr__(self) -> str:
        return f"ConsentNotice(event={self.event!r}, record_id={self.record_id!r})"


def notice_delivery(notice: ConsentNotice, first_submitted_at: datetime) -> SmsDelivery:
    if notice.event not in NOTICE_TEMPLATES:
        raise ValueError(f"unknown consent notice {notice.event!r}")
    fields = {
        "partner": notice.partner,
        "ref": short_ref(notice.record_id),
        "until": notice.until or "",
        "who": notice.who or "",
    }
    for name, value in fields.items():
        if len(value) > FIELD_LIMITS[name]:
            raise TemplateTooLong(f"{name} longer than {FIELD_LIMITS[name]} characters")
    text = _fit(NOTICE_TEMPLATES[notice.event].format(**fields), notice.event)
    return SmsDelivery(
        # One notice per record and event: Consent Manager ids are UUIDs, never reused.
        idempotency_key=delivery_key("dpi-consent", notice.record_id, notice.event, "sms"),
        recipient=notice.phone,
        text=text,
        source_reference=f"dpi-consent:{notice.record_id}:{notice.event}",
        purpose="transactional",
        expires_at=None,
        first_submitted_at=first_submitted_at,
    )


class OanOtpSender:
    """Drop-in delivery step for Consent Manager's ``OtpService``.

    Call ``send`` right after the OTP is stored (hash, TTL, attempts). Returns ``None``
    when no phone is known; the caller must then fail the consent step visibly. The client
    retries uncertain outcomes itself, always with the same key and body.
    """

    def __init__(self, client: OanClient, phones: PhoneResolver) -> None:
        self._client = client
        self._phones = phones

    async def send(
        self,
        *,
        kind: str,
        transaction_id: str,
        issue_no: int,
        code: str,
        partner: str,
        subject_id_type: str,
        subject_id_value: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> Submission | None:
        phone = await self._phones.phone_for(subject_id_type, subject_id_value)
        if phone is None:
            return None
        msg = OtpMessage(kind, transaction_id, issue_no, code, partner, expires_at, phone)
        return await self._client.submit(otp_delivery(msg, issued_at))
