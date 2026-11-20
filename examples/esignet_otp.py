"""eSignet: deliver an OTP message supplied by the identity system.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION. Nothing here modifies the
eSignet repository. The eSignet team would call OAN from its own notification path.

Ownership (unchanged by OAN):

* eSignet generates the OTP, looks up the linked phone, sets the expiry, applies its
  resend rules and verifies the OTP.
* OAN only delivers the text eSignet supplies. ``accepted``, ``sent`` or even ``delivered``
  never proves that an identity was verified.
* OAN never extends ``expires_at`` and never creates a replacement OTP. A user-requested
  resend is eSignet's decision: a NEW OTP is a new delivery, with a new ``otp_sequence``
  and therefore a new key. It is not a retry.

ASSUMED event schema (no eSignet notification schema was available). The required
mapping must be confirmed with the eSignet team:

=================  ========================================================
transaction_id     opaque eSignet transaction id (must not contain PII)
otp_sequence       eSignet's counter per transaction (1 for the first OTP)
phone_e164         linked phone selected by eSignet (+2519…/+2517… or 09…/07…)
message_text       fully rendered SMS text including the OTP, one SMS part
expires_at         eSignet's OTP expiry (aware datetime, <= OAN's 30-min ceiling)
=================  ========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from oan_client import OanClient, SmsDelivery, Submission, delivery_key


@dataclass(frozen=True, repr=False)
class OtpNotification:
    transaction_id: str
    otp_sequence: int
    phone_e164: str
    message_text: str
    expires_at: datetime

    def __repr__(self) -> str:  # never print the OTP text or the phone
        return f"OtpNotification(transaction_id={self.transaction_id!r}, seq={self.otp_sequence})"


def to_delivery(event: OtpNotification, first_submitted_at: datetime) -> SmsDelivery:
    """Map one eSignet OTP notification to exactly one OAN delivery."""
    return SmsDelivery(
        idempotency_key=delivery_key("esignet", event.transaction_id,
                                     f"otp-{event.otp_sequence}", "sms"),
        recipient=event.phone_e164,
        text=event.message_text,
        source_reference=f"esignet:otp:{event.transaction_id}",
        purpose="otp",
        expires_at=event.expires_at,  # passed through unchanged, never extended
        first_submitted_at=first_submitted_at,
    )


async def deliver_otp(client: OanClient, event: OtpNotification,
                      first_submitted_at: datetime) -> Submission:
    """Submit (or safely replay) the OTP delivery. The caller persists ``event`` and
    ``first_submitted_at`` so a later retry rebuilds the identical delivery."""
    return await client.submit(to_delivery(event, first_submitted_at))
