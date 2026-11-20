"""Auth: phone-only password-reset OTP, sent through OAN.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION. Nothing here changes the
Auth repository; the Auth team would make the equivalent change in its own code.

Matches ``oan_auth_service`` develop ``34c7306``, ``api/v1/auth.py:_deliver_reset_by_sms``.
Auth keeps every rule it has today, unchanged:

* it generates a numeric OTP (``password_reset_otp_length``, default 6 digits) and stores
  only its pbkdf2 hash, with ``password_reset_otp_ttl`` (default 600 s);
* it enforces its issue cap (``password_reset_otp_max_issued_per_day``) and attempt cap
  (``password_reset_otp_max_attempts``), and verifies the code itself
  (``_verify_password_reset_otp``);
* it renders its own template, "{0} is your password reset code. It expires in {1}
  minutes.", and stays silent toward the requester whatever the outcome;
* it queues the send after commit (``enqueue_after_commit=True``), so a rolled-back
  request never sends a code it did not store.

Only the transport changes: the post-commit job calls OAN instead of Frappe ``_send_sms``.
A generic Frappe ``send_sms`` hook could not do this, because it receives only numbers
and text, with no stable delivery identity, purpose or deadline.

Mapping:

==================  ====================================================================
Idempotency-Key     ``delivery_id``: a random UUID4 Auth creates when it issues this OTP
                    and passes in the job arguments. Never derived from the OTP, phone or
                    user. Every retry of this job reuses it.
purpose             ``otp`` (OAN dispatches OTPs first)
expires_at          Auth's own OTP deadline, ``issued_at + TTL``, passed through
                    unchanged. Contract ceiling for ``otp`` is 30 minutes; the 600 s
                    default fits. A larger configured TTL is refused here, not shortened.
source_reference    ``auth:password-reset:<reset_request_id>`` (opaque Auth id, no PII)
first_submitted_at  ``issued_at`` (persisted in the job arguments)
==================  ====================================================================

Retries and resends. The client retries uncertain outcomes inside the job only, with the
same key and identical bytes. If the job ends with ``OanUncertain`` or
``OanRetryDeferred``, Auth does not issue a new code automatically: a transport failure
never generates a replacement OTP. The user may ask again under Auth's resend rules; that
is a new OTP, a new ``delivery_id`` and a new key, and Auth's cache already invalidates
the previous code. OAN never sends the old message after its ``expires_at``.

Storage. The rendered text (which contains the OTP) exists only in the post-commit job's
arguments for the job's lifetime, as it does today with ``_send_sms``. It is not written
to an outbox table or a log. OAN stores it encrypted and purges it after the message is
final. No status polling is needed: the user either receives the code or asks again.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from oan_client import OanClient, SmsDelivery, Submission

#: Contract 3.0.0 ceiling for ``purpose=otp`` expiry.
OTP_MAX_LIFETIME = timedelta(minutes=30)


def new_delivery_id() -> str:
    """Called by Auth once, when it issues the OTP (before enqueueing the job)."""
    return str(uuid.uuid4())


@dataclass(frozen=True, repr=False)
class ResetOtpIssued:
    """Arguments of Auth's post-commit send job for one issued OTP."""

    delivery_id: str  #: UUID4 from ``new_delivery_id()``; the Idempotency-Key
    reset_request_id: str  #: opaque Auth id for correlation (no phone, no user name)
    mobile_no: str  #: the account's phone, selected by Auth
    message_text: str  #: Auth's rendered template, including the OTP
    issued_at: datetime  #: when Auth stored the OTP hash
    otp_expires_at: datetime  #: ``issued_at + password_reset_otp_ttl``

    def __repr__(self) -> str:  # never the OTP text or the phone
        return f"ResetOtpIssued(reset_request_id={self.reset_request_id!r})"


def to_delivery(event: ResetOtpIssued) -> SmsDelivery:
    """Map one issued reset OTP to exactly one OAN delivery."""
    if uuid.UUID(event.delivery_id).version != 4:
        raise ValueError("delivery_id must be a random UUID4 created when the OTP is issued")
    lifetime = event.otp_expires_at - event.issued_at
    if not timedelta(0) < lifetime <= OTP_MAX_LIFETIME:
        raise ValueError("OTP lifetime must be positive and at most 30 minutes")
    return SmsDelivery(
        idempotency_key=event.delivery_id,
        recipient=event.mobile_no,
        text=event.message_text,
        source_reference=f"auth:password-reset:{event.reset_request_id}",
        purpose="otp",
        expires_at=event.otp_expires_at,  # Auth's deadline, never extended
        first_submitted_at=event.issued_at,
    )


async def send_reset_otp(client: OanClient, event: ResetOtpIssued) -> Submission:
    """Body of Auth's post-commit job. A 202/200 means OAN ACCEPTED the message, not that
    it was sent or delivered. Exceptions are logged by Auth without the text or phone
    and are not reported to the requester."""
    return await client.submit(to_delivery(event))
