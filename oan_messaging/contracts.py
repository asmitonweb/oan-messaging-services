"""Frozen contracts for the OAN Messaging Service (MSG-01).

Everything another task builds against lives here:

* API submission / response models (mirrored by ``docs/openapi.yaml``)
* idempotency, canonical payloads and keyed fingerprints
* the public message lifecycle, allowed transitions and content-purge rule
* the adapter interface (MSG-03 8028 SMS, MSG-04 Telegram)
* normalized provider results and 8028 delivery receipts
* the persistence interface (implemented by MSG-02, driven by MSG-05)
* configurable service policy defaults

Changing anything in this module after MSG-01 is accepted requires a contract
change request; downstream bots must not edit it.

Outcome vocabulary (the four words that must never be conflated):

``accepted``
    The service durably stored the request. Nothing has left the service.
``sent``
    The provider confirmed it took the message (8028 ``success: true`` with an
    ``sms_id``; Telegram ``ok: true`` with a ``message_id``). Not a handset claim.
``delivered``
    The 8028 gateway reported handset delivery. Telegram never reaches this state:
    the Bot API has no delivery receipts in this scope.
``unknown``
    A gateway handoff was attempted and the service cannot prove whether the provider
    took it (timeout, 5xx, malformed or partial success body, crash mid-call). Unknown
    messages are never resent automatically.

Two kinds of retry must not be confused:

* **API replay (safe).** A caller whose ``POST`` timed out or got an uncertain response
  retries with the *same* ``Idempotency-Key`` and unchanged body. The service either
  returns the original message (200) or accepts it once (202).
* **Gateway resend (unsafe).** After an ambiguous provider call the service does *not*
  resend; the message stays ``unknown``.

Run ``python -m oan_messaging.contracts`` to validate the built-in examples, the
idempotency/fingerprint/purge rules, and the examples in ``docs/openapi.yaml``.
The self-check makes no network calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

CONTRACT_VERSION = "3.2.0"

# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class Channel(StrEnum):
    SMS = "sms"
    TELEGRAM = "telegram"


#: Whether a channel can ever report handset delivery in this scope.
CHANNEL_DELIVERY_RECEIPTS: Mapping[Channel, bool] = {
    Channel.SMS: True,  # 8028 status callbacks
    Channel.TELEGRAM: False,  # Bot API: sent is the strongest outcome
}


class Purpose(StrEnum):
    """Why a message is sent. Callers are permitted per purpose (MSG-02)."""

    OTP = "otp"  # one-time passcode; expires_at mandatory; highest priority
    TRANSACTIONAL = "transactional"  # response to a user action (grievance update, visit)
    ALERT = "alert"  # time-sensitive advisory (e.g. weather)
    BROADCAST = "broadcast"  # bulk advisory; lowest priority


#: Lower number = claimed first by workers (MSG-05). OTP always beats broadcast.
PURPOSE_PRIORITY: Mapping[Purpose, int] = {
    Purpose.OTP: 0,
    Purpose.TRANSACTIONAL: 10,
    Purpose.ALERT: 20,
    Purpose.BROADCAST: 30,
}

#: Upper bound on a provider's ``retry_after`` that the service honours. A larger
#: value (a misbehaving upstream) would otherwise pause a channel for years or
#: overflow the date arithmetic and stop the worker.
MAX_RETRY_AFTER_SECONDS = 3600


class MessageStatus(StrEnum):
    """Public lifecycle state returned by ``GET /api/v1/messages/{id}``."""

    ACCEPTED = "accepted"  # stored durably; not yet handed to a provider
    SENDING = "sending"  # a worker committed an attempt and is calling the provider
    SENT = "sent"  # provider confirmed acceptance; NOT a handset delivery claim
    DELIVERED = "delivered"  # 8028 reported handset delivery (SMS only)
    FAILED = "failed"  # definite non-delivery (rejected, blocked, gateway expiry)
    EXPIRED = "expired"  # expires_at passed before any handoff; never left the service
    UNKNOWN = "unknown"  # handoff attempted, provider acceptance cannot be proven


#: States from which no further transition is allowed.
FINAL_STATUSES: frozenset[MessageStatus] = frozenset(
    {MessageStatus.DELIVERED, MessageStatus.FAILED, MessageStatus.EXPIRED}
)

#: The only legal transitions. Storage (MSG-02) must enforce these; anything else is a
#: bug or an out-of-order receipt and must be recorded without changing status.
ALLOWED_TRANSITIONS: Mapping[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.ACCEPTED: frozenset({MessageStatus.SENDING, MessageStatus.EXPIRED}),
    MessageStatus.SENDING: frozenset(
        {
            MessageStatus.SENT,
            MessageStatus.FAILED,
            MessageStatus.UNKNOWN,
            # Only when the attempt is proven not to have reached the provider
            # (ProviderOutcome.NOT_SENT): back to the queue for a later attempt.
            MessageStatus.ACCEPTED,
            # NOT_SENT and the message expired while waiting to retry.
            MessageStatus.EXPIRED,
        }
    ),
    MessageStatus.SENT: frozenset({MessageStatus.DELIVERED, MessageStatus.FAILED}),
    # A late provider signal (e.g. a receipt correlated by provider id) may resolve an
    # unknown outcome. Unknown never goes back to accepted: no blind resend.
    MessageStatus.UNKNOWN: frozenset(
        {MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.FAILED}
    ),
    MessageStatus.DELIVERED: frozenset(),
    MessageStatus.FAILED: frozenset(),
    MessageStatus.EXPIRED: frozenset(),
}


def is_allowed_transition(current: MessageStatus, new: MessageStatus) -> bool:
    return new in ALLOWED_TRANSITIONS[current]


#: Statuses in which message content (text + recipient ciphertext) may still be needed
#: for a send. Content MUST be purged as soon as a message leaves these statuses —
#: including ``sent``, which for Telegram never becomes final and for SMS may never get
#: a receipt. Purge is unrelated to receipt finality.
CONTENT_REQUIRED_STATUSES: frozenset[MessageStatus] = frozenset(
    {MessageStatus.ACCEPTED, MessageStatus.SENDING}
)
PURGEABLE_STATUSES: frozenset[MessageStatus] = frozenset(MessageStatus) - CONTENT_REQUIRED_STATUSES


class ProviderOutcome(StrEnum):
    """What a single adapter ``send`` call proved. Mapped to status by the worker."""

    SENT = "sent"  # provider took it; provider_message_id present -> SENT
    REJECTED = "rejected"  # definite refusal, will not succeed unchanged -> FAILED
    NOT_SENT = "not_sent"  # provably never reached the provider -> ACCEPTED (retry) or FAILED
    UNKNOWN = "unknown"  # cannot prove either way -> UNKNOWN; never auto-resend


#: Worker mapping from adapter outcome to the next message status.
#: NOT_SENT maps to ACCEPTED when ``retryable`` and not expired, otherwise FAILED/EXPIRED.
OUTCOME_TO_STATUS: Mapping[ProviderOutcome, MessageStatus] = {
    ProviderOutcome.SENT: MessageStatus.SENT,
    ProviderOutcome.REJECTED: MessageStatus.FAILED,
    ProviderOutcome.NOT_SENT: MessageStatus.ACCEPTED,
    ProviderOutcome.UNKNOWN: MessageStatus.UNKNOWN,
}


class Provider(StrEnum):
    SMS_8028 = "sms8028"
    TELEGRAM = "telegram"


class FailureCode(StrEnum):
    """Normalized, non-sensitive reason codes. Raw provider status is kept separately.

    Codes describe what the provider *reported*, not a proven root cause.
    """

    INVALID_RECIPIENT = "invalid_recipient"
    TEXT_REJECTED = "text_rejected"
    RECIPIENT_BLOCKED_BOT = "recipient_blocked_bot"  # Telegram 403 bot was blocked
    CHAT_NOT_FOUND = "chat_not_found"  # Telegram 400 chat not found / user never started bot
    PROVIDER_REJECTED = "provider_rejected"  # 8028 callback Rejected or other definite refusal
    #: 8028 callback ``Expired``: the gateway reports the delivery validity period ran out.
    #: It does not prove why (handset off, out of coverage, operator issue, ...).
    PROVIDER_DELIVERY_EXPIRED = "provider_delivery_expired"
    PROVIDER_AUTH = "provider_auth"  # 401/403 from provider: our credentials; ops issue
    PROVIDER_CONFIG = "provider_config"  # TLS/cert/host misconfiguration; nothing sent
    RATE_LIMITED = "rate_limited"  # provider asked us to slow down (Telegram 429)
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # connection refused before any request
    TIMEOUT = "timeout"  # ambiguous
    PROVIDER_SERVER_ERROR = "provider_server_error"  # 5xx: ambiguous
    MALFORMED_RESPONSE = "malformed_response"  # ambiguous
    INTERRUPTED = "interrupted"  # worker crash / lease lost mid-call: ambiguous
    EXPIRED_BEFORE_SEND = "expired_before_send"


# --------------------------------------------------------------------------------------
# Service policy (configurable defaults; MSG-02/05 load overrides from configuration)
# --------------------------------------------------------------------------------------


class ServicePolicy(BaseModel):
    """Configurable service policy. Defaults are OAN service limits, not identity rules.

    OAN never extends or invents a caller's ``expires_at``; it only rejects values outside
    these ceilings. The OTP ceiling does not validate an identity provider's OTP lifetime.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_otp_ttl: timedelta = timedelta(minutes=30)
    max_expiry_horizon: timedelta = timedelta(days=7)
    #: Minimum guarantee window for an Idempotency-Key binding, from ``created_at``.
    #: Bindings of outstanding messages (accepted/sending/unknown) are never released;
    #: a completed message's binding may be released only after this window.
    dedup_window: timedelta = timedelta(days=30)
    #: How long 8028 receipts are still matched, from the receipt anchor (``sent_at``, else
    #: the last persisted attempt time). The recipient fingerprint is retained this long.
    #: Later receipts are recorded as ``unmatched_late``.
    receipt_window: timedelta = timedelta(days=7)
    #: Stored receipt evidence (sms_id, recipient fingerprint, masked phone, raw status,
    #: times) is deleted after this. Raw phone numbers are never persisted at all.
    receipt_evidence_retention: timedelta = timedelta(days=90)
    #: Content sweeper period. Target: content of a purgeable message is gone within
    #: ``worker_lease + purge_sweep_interval`` of the send outcome, *while workers and the
    #: database are available*. An outage delays cleanup; it resumes on recovery.
    purge_sweep_interval: timedelta = timedelta(minutes=5)
    worker_lease: timedelta = timedelta(minutes=2)

    @model_validator(mode="after")
    def _coherent(self) -> ServicePolicy:
        for name in type(self).model_fields:
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be a positive duration")
        if self.max_otp_ttl > self.max_expiry_horizon:
            raise ValueError("max_otp_ttl must not exceed max_expiry_horizon")
        if self.dedup_window < self.max_expiry_horizon + timedelta(days=1):
            raise ValueError("dedup_window must exceed max_expiry_horizon by at least 1 day")
        if self.receipt_evidence_retention < self.receipt_window:
            raise ValueError("receipt_evidence_retention must cover receipt_window")
        return self

    @property
    def content_retention_target_after_send(self) -> timedelta:
        """Operational target, not an absolute guarantee: assumes workers and database
        are up. During an outage content waits; the sweeper catches up on recovery."""
        return self.worker_lease + self.purge_sweep_interval


DEFAULT_POLICY = ServicePolicy()


# --------------------------------------------------------------------------------------
# Field rules shared by the API (MSG-02) and adapters (MSG-03/04)
# --------------------------------------------------------------------------------------

#: Business correlation (source event ID). NOT unique: one event may fan out to several
#: deliveries that share it.
SOURCE_REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$"
SourceReference = Annotated[str, StringConstraints(pattern=SOURCE_REFERENCE_PATTERN)]

#: ``Idempotency-Key`` request header. One stable key per intended delivery; every retry
#: of that delivery reuses it. Scope: (caller_id, channel).
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{7,127}$"
IdempotencyKey = Annotated[str, StringConstraints(pattern=IDEMPOTENCY_KEY_PATTERN)]

#: Accepted on input: +2519XXXXXXXX, 2519XXXXXXXX, 09XXXXXXXX, 07XXXXXXXX (also 7/9 series
#: with +251). Stored and passed to adapters as E.164 (+2519XXXXXXXX / +2517XXXXXXXX).
ET_MOBILE_INPUT_PATTERN = r"^(\+?251[79][0-9]{8}|0[79][0-9]{8})$"
ET_MOBILE_E164_PATTERN = r"^\+251[79][0-9]{8}$"
E164EthiopianMobile = Annotated[str, StringConstraints(pattern=ET_MOBILE_E164_PATTERN)]


def normalize_et_mobile(raw: str) -> str:
    """Return the canonical E.164 form or raise ``ValueError`` (message never echoes input)."""
    candidate = raw.strip()
    if not re.fullmatch(ET_MOBILE_INPUT_PATTERN, candidate):
        raise ValueError("recipient must be an Ethiopian mobile number (09/07 series)")
    digits = candidate.lstrip("+")
    if digits.startswith("0"):
        digits = "251" + digits[1:]
    return "+" + digits


def mask_recipient(e164: str) -> str:
    """Status responses and logs only ever show a masked recipient: +2519******58."""
    return e164[:5] + "*" * (len(e164) - 7) + e164[-2:]


def mask_chat_id(chat_id: int) -> str:
    s = str(abs(chat_id))
    return "chat:" + "*" * max(len(s) - 2, 1) + s[-2:]


# GSM 03.38 basic character set (1 septet) and extension set (2 septets).
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")

#: One part holds 160 GSM-7 septets or 70 UTF-16 units. A longer text is sent as a
#: concatenated message whose parts each lose a 6-byte header: 153 septets or 67 units
#: (SMS-09, 20 Nov 2026). 8028's guide v1.1 sets no length limit and the handset joins
#: the parts; we cap a message at SMS_MAX_PARTS parts. An OTP is always one part.
SMS_SINGLE_PART_GSM7_SEPTETS = 160
SMS_SINGLE_PART_UCS2_UNITS = 70
SMS_MULTIPART_GSM7_SEPTETS = 153
SMS_MULTIPART_UCS2_UNITS = 67
SMS_MAX_PARTS = 3

#: Telegram sendMessage limit (after entity parsing). Plain text, no parse_mode.
#: Counted in UTF-16 code units to be conservative for astral characters (emoji).
TELEGRAM_MAX_TEXT_UTF16_UNITS = 4096


def sms_encoding(text: str) -> Literal["gsm7", "ucs2"]:
    return "gsm7" if all(c in _GSM7_BASIC or c in _GSM7_EXTENSION for c in text) else "ucs2"


def sms_length_units(text: str) -> tuple[Literal["gsm7", "ucs2"], int, int]:
    """Return (encoding, units used, single-part capacity)."""
    enc = sms_encoding(text)
    if enc == "gsm7":
        used = sum(2 if c in _GSM7_EXTENSION else 1 for c in text)
        return enc, used, SMS_SINGLE_PART_GSM7_SEPTETS
    used = len(text.encode("utf-16-le")) // 2
    return enc, used, SMS_SINGLE_PART_UCS2_UNITS


def sms_parts(text: str) -> int:
    """How many SMS parts ``text`` needs (1 for an empty or short text).

    Counted as a gateway splits it: a GSM-7 extension character (2 septets) or a
    UTF-16 surrogate pair (2 units) is never cut in half, so it moves whole to the
    next part. Clients that only estimate (160/153, 70/67) may count one part fewer
    in those rare cases; the service's count is the one that is enforced.
    """
    enc = sms_encoding(text)
    if enc == "gsm7":
        sizes = [2 if c in _GSM7_EXTENSION else 1 for c in text]
        single, per_part = SMS_SINGLE_PART_GSM7_SEPTETS, SMS_MULTIPART_GSM7_SEPTETS
    else:
        sizes = [len(c.encode("utf-16-le")) // 2 for c in text]
        single, per_part = SMS_SINGLE_PART_UCS2_UNITS, SMS_MULTIPART_UCS2_UNITS
    if sum(sizes) <= single:
        return 1
    parts, room = 1, per_part
    for size in sizes:
        if size > room:
            parts, room = parts + 1, per_part
        room -= size
    return parts


_DISALLOWED_CONTROL = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]")


def _check_plain_text(text: str) -> str:
    if not text.strip():
        raise ValueError("text must not be blank")
    if _DISALLOWED_CONTROL.search(text):
        raise ValueError("text contains control characters")
    return text


def check_expiry(
    purpose: Purpose,
    expires_at: datetime | None,
    now: datetime,
    policy: ServicePolicy = DEFAULT_POLICY,
) -> None:
    """Time-sensitive rule for a **new** acceptance only.

    Not applied to an identical replay of an existing key (see
    ``classify_idempotent_request``): a replay after the original expiry returns the
    original message. Never modifies ``expires_at``.
    """
    if expires_at is None:
        if purpose is Purpose.OTP:
            raise ValueError("expires_at is required when purpose is otp")
        return
    if expires_at <= now:
        raise ValueError("expires_at must be in the future")
    horizon = policy.max_otp_ttl if purpose is Purpose.OTP else policy.max_expiry_horizon
    if expires_at - now > horizon:
        raise ValueError("expires_at is too far in the future for this purpose")


# --------------------------------------------------------------------------------------
# API models (request bodies, responses). Mirrored exactly by docs/openapi.yaml.
# --------------------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class _SubmissionBase(_Frozen):
    source_reference: SourceReference
    purpose: Purpose
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _otp_requires_expiry(self) -> _SubmissionBase:
        # Static rule only; "is it in the future" needs the server clock -> check_expiry().
        if self.purpose is Purpose.OTP and self.expires_at is None:
            raise ValueError("expires_at is required when purpose is otp")
        return self


class SmsSubmission(_SubmissionBase):
    """Body of ``POST /api/v1/messages/sms``. One recipient per message."""

    recipient: str
    text: str

    @field_validator("recipient")
    @classmethod
    def _recipient(cls, v: str) -> str:
        return normalize_et_mobile(v)

    @field_validator("text")
    @classmethod
    def _text(cls, v: str, info: ValidationInfo) -> str:
        _check_plain_text(v)
        parts = sms_parts(v)
        if parts > 1 and info.data.get("purpose") is Purpose.OTP:
            raise ValueError("an otp text exceeds a single SMS part for its encoding")
        if parts > SMS_MAX_PARTS:
            raise ValueError("text exceeds the sms part limit for its encoding")
        return v


class TelegramSubmission(_SubmissionBase):
    """Body of ``POST /api/v1/messages/telegram``.

    ``chat_id`` must already be known: the recipient has started the caller's bot.
    The bot identity is taken from the authenticated caller's configuration, never
    from the request; there is no token or URL field.
    """

    chat_id: Annotated[int, Field(strict=True)]
    text: str

    @field_validator("chat_id")
    @classmethod
    def _chat_id(cls, v: int) -> int:
        if v == 0 or abs(v) >= 2**52:
            raise ValueError("chat_id is not a valid Telegram chat identifier")
        return v

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        _check_plain_text(v)
        if len(v.encode("utf-16-le")) // 2 > TELEGRAM_MAX_TEXT_UTF16_UNITS:
            raise ValueError("text exceeds the Telegram message length limit")
        return v


Submission = SmsSubmission | TelegramSubmission


def submission_channel(submission: Submission) -> Channel:
    return Channel.SMS if isinstance(submission, SmsSubmission) else Channel.TELEGRAM


class SubmissionResponse(_Frozen):
    """202 on first acceptance; 200 with the same id on an identical replay."""

    id: uuid.UUID
    idempotency_key: IdempotencyKey
    channel: Channel
    status: MessageStatus
    source_reference: SourceReference
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None


class MessageStatusView(_Frozen):
    """Body of ``GET /api/v1/messages/{id}``. Never contains message text."""

    id: uuid.UUID
    idempotency_key: IdempotencyKey
    channel: Channel
    purpose: Purpose
    source_reference: SourceReference
    status: MessageStatus
    final: bool
    failure_code: FailureCode | None = None
    #: Last raw provider status (e.g. 8028 ``Delivered``/``Expired``, ``http_503``).
    provider_status: str | None = None
    #: Masked phone (SMS) or masked chat id (Telegram).
    recipient_masked: str
    #: Must equal CHANNEL_DELIVERY_RECEIPTS[channel]: true for SMS, false for Telegram.
    delivery_receipts_supported: bool
    attempts: Annotated[int, Field(ge=0)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    delivered_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> MessageStatusView:
        if self.final != (self.status in FINAL_STATUSES):
            raise ValueError("final must match status")
        if self.delivery_receipts_supported != CHANNEL_DELIVERY_RECEIPTS[self.channel]:
            raise ValueError("delivery_receipts_supported does not match the channel")
        if self.channel is Channel.TELEGRAM and (
            self.status is MessageStatus.DELIVERED or self.delivered_at is not None
        ):
            raise ValueError("telegram messages can never be delivered")
        if self.status is MessageStatus.DELIVERED and self.delivered_at is None:
            raise ValueError("delivered requires delivered_at")
        if self.status is MessageStatus.FAILED and self.failure_code is None:
            raise ValueError("failed requires failure_code")
        return self


class ErrorItem(_Frozen):
    #: Field location, e.g. ``recipient`` or ``header.Idempotency-Key``. Never the value.
    field: str | None = None
    code: str


class Problem(_Frozen):
    """RFC 9457-style error body. Redacted: never echoes submitted values."""

    type: str = "about:blank"
    title: str
    status: int
    code: Literal[
        "unauthenticated",
        "forbidden",
        "not_found",
        "validation_failed",
        "idempotency_conflict",
        "payload_too_large",
        "unsupported_media_type",
        "service_unavailable",
    ]
    errors: tuple[ErrorItem, ...] = ()


# --------------------------------------------------------------------------------------
# Caller identity (resolved by MSG-02 auth, consumed by API + worker)
# --------------------------------------------------------------------------------------


class CallerContext(_Frozen):
    caller_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    allowed_channels: frozenset[Channel]
    allowed_purposes: frozenset[Purpose]
    #: Configured bot key for Telegram sends (maps to a secret token in config). None means
    #: the caller may not use Telegram even if the channel is listed.
    telegram_bot: str | None = None
    #: May read GET /api/v1/charts (statistics). Readers have no channels or purposes.
    can_read_stats: bool = False


# --------------------------------------------------------------------------------------
# Canonical payloads and keyed fingerprints
# --------------------------------------------------------------------------------------
#
# Content is purged early, so equality and receipt matching use retained, versioned,
# keyed HMAC-SHA256 fingerprints. A plain hash of a phone number or a 6-digit OTP is
# brute-forceable; an HMAC under a secret key held outside the database is not.
#
# Key handling (MSG-02):
# * Keys are >= 32 random bytes from secret configuration, identified by a version id.
# * New rows use the current version; each row stores the version it used.
# * Verification recomputes with the row's version.
# * A key version may be retired only when no retained fingerprint still depends on it
#   (``can_retire_key_version``): a live binding's payload fingerprint, an SMS recipient
#   fingerprint before its match deadline, or a stored receipt within evidence retention.
#   Elapsed time since rotation alone is never sufficient (messages can stay outstanding,
#   e.g. ``unknown``, indefinitely).


class FingerprintDomain(StrEnum):
    PAYLOAD = "payload"  # canonical submission (idempotency equality)
    RECIPIENT = "recipient"  # normalized E.164 (8028 receipt matching)


KEY_VERSION_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class KeyedFingerprint(_Frozen):
    key_version: Annotated[str, StringConstraints(pattern=KEY_VERSION_PATTERN)]
    domain: FingerprintDomain
    #: Lowercase hex HMAC-SHA256.
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    def matches(self, other: KeyedFingerprint) -> bool:
        if (self.key_version, self.domain) != (other.key_version, other.domain):
            raise ValueError("fingerprints must be compared under the same key version/domain")
        return hmac.compare_digest(self.digest, other.digest)


MIN_FINGERPRINT_KEY_BYTES = 32
CANONICAL_PAYLOAD_VERSION = 1


def keyed_fingerprint(
    key: bytes, key_version: str, domain: FingerprintDomain, data: bytes
) -> KeyedFingerprint:
    """HMAC-SHA256 with domain separation. The frozen algorithm for all fingerprints."""
    if len(key) < MIN_FINGERPRINT_KEY_BYTES:
        raise ValueError("fingerprint key too short")
    message = f"oan-msg/{domain.value}/v1".encode() + b"\x00" + data
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return KeyedFingerprint(key_version=key_version, domain=domain, digest=digest)


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_payload(caller_id: str, submission: Submission) -> bytes:
    """Deterministic bytes for payload equality.

    Fields: caller_id, channel, purpose, source_reference, recipient (E.164, after
    normalization) or chat_id, text (byte-exact; no Unicode normalization) and
    expires_at (UTC, microseconds). The Idempotency-Key itself is the lookup key and
    is not part of the payload.
    """
    body: dict[str, Any] = {
        "v": CANONICAL_PAYLOAD_VERSION,
        "caller_id": caller_id,
        "channel": submission_channel(submission).value,
        "purpose": submission.purpose.value,
        "source_reference": submission.source_reference,
        "text": submission.text,
        "expires_at": _utc_iso(submission.expires_at),
    }
    if isinstance(submission, SmsSubmission):
        body["recipient"] = submission.recipient
    else:
        body["chat_id"] = submission.chat_id
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_recipient(e164: str) -> bytes:
    return normalize_et_mobile(e164).encode()


class Fingerprinter(Protocol):
    """Holds the versioned keys (MSG-02). Reference implementation: ``HmacFingerprinter``."""

    @property
    def current_version(self) -> str: ...

    def fingerprint(
        self, domain: FingerprintDomain, data: bytes, key_version: str | None = None
    ) -> KeyedFingerprint: ...


class HmacFingerprinter:
    def __init__(self, keys: Mapping[str, bytes], current_version: str) -> None:
        if current_version not in keys:
            raise ValueError("current key version missing")
        self._keys = dict(keys)
        self._current = current_version

    @property
    def current_version(self) -> str:
        return self._current

    def fingerprint(
        self, domain: FingerprintDomain, data: bytes, key_version: str | None = None
    ) -> KeyedFingerprint:
        version = key_version or self._current
        try:
            key = self._keys[version]
        except KeyError:
            raise ValueError("unknown or retired fingerprint key version") from None
        return keyed_fingerprint(key, version, domain, data)


class IdempotencyDecision(StrEnum):
    NEW = "new"  # no live binding: validate expiry, accept, 202
    REPLAY = "replay"  # identical payload: return original message, 200
    CONFLICT = "conflict"  # different payload under the same key: 409


#: Messages that may still be (or may already have been) submitted to a provider. Their
#: idempotency binding and payload fingerprint are retained regardless of age: releasing
#: them could create a second copy while the first is pending, or repeat a delivery whose
#: outcome is unproven.
OUTSTANDING_BINDING_STATUSES: frozenset[MessageStatus] = frozenset(
    {MessageStatus.ACCEPTED, MessageStatus.SENDING, MessageStatus.UNKNOWN}
)
#: No further provider submission can happen. ``sent`` belongs here: the provider accepted
#: it, nothing more will be sent, even if no final receipt ever arrives.
COMPLETED_BINDING_STATUSES: frozenset[MessageStatus] = (
    frozenset(MessageStatus) - OUTSTANDING_BINDING_STATUSES
)


def classify_idempotent_request(
    existing: StoredMessage | None,
    candidate_payload_fp: KeyedFingerprint | None,
) -> IdempotencyDecision:
    """Decide a submission against the row found for (caller, channel, key).

    Age is irrelevant here: a live binding always yields REPLAY or CONFLICT. Only a row
    whose binding storage has *released* (``binding_released_at`` set, which the model
    allows only for completed messages past ``dedup_window``) leaves the key free.
    ``candidate_payload_fp`` must be computed under the row's key version. Works after
    content purge and after the original ``expires_at``.
    """
    if existing is None or existing.binding_released_at is not None:
        return IdempotencyDecision.NEW
    if candidate_payload_fp is None:
        raise ValueError("candidate fingerprint required when a binding exists")
    assert existing.payload_fingerprint is not None  # guaranteed by StoredMessage
    if existing.payload_fingerprint.matches(candidate_payload_fp):
        return IdempotencyDecision.REPLAY
    return IdempotencyDecision.CONFLICT


def binding_release_due(
    message: StoredMessage, now: datetime, policy: ServicePolicy = DEFAULT_POLICY
) -> bool:
    """True when ``purge_due`` may release the binding (drop the payload fingerprint and
    free the key): the message is completed AND the guarantee window has elapsed."""
    return (
        message.binding_released_at is None
        and message.status in COMPLETED_BINDING_STATUSES
        and now >= message.created_at + policy.dedup_window
    )


def receipt_anchor(message: StoredMessage) -> datetime | None:
    """Start of the receipt-matching window.

    ``sent_at`` if present, otherwise the last persisted attempt time (unknown, rejected,
    interrupted attempts), otherwise ``created_at`` for messages that never had an attempt
    (e.g. expired before send). None while a send is still possible (accepted/sending).
    """
    if message.status in CONTENT_REQUIRED_STATUSES:
        return None
    return message.sent_at or message.last_attempt_at or message.created_at


def recipient_match_deadline(
    message: StoredMessage, policy: ServicePolicy = DEFAULT_POLICY
) -> datetime | None:
    """When the SMS recipient fingerprint may be released. None = keep (or not SMS).

    Releasing it for an ``unknown`` message only stops late receipt matching; the message
    stays ``unknown`` and is never resent, and its binding is still retained.
    """
    if message.channel is not Channel.SMS:
        return None
    anchor = receipt_anchor(message)
    return None if anchor is None else anchor + policy.receipt_window


def can_retire_key_version(version: str, current: str, in_use: frozenset[str]) -> bool:
    """A key version may be destroyed only if it is not current and no retained
    fingerprint depends on it (see ``MessageRepository.fingerprint_key_versions_in_use``)."""
    return version != current and version not in in_use


# --------------------------------------------------------------------------------------
# Adapter interface (MSG-03, MSG-04)
# --------------------------------------------------------------------------------------


class SmsSendRequest(_Frozen):
    channel: Literal[Channel.SMS] = Channel.SMS
    message_id: uuid.UUID
    attempt: Annotated[int, Field(ge=1)]
    # The adapter converts to 09/07 domestic numbers for 8028.
    recipient: E164EthiopianMobile = Field(repr=False)
    text: str = Field(repr=False)
    expires_at: AwareDatetime | None = None


class TelegramSendRequest(_Frozen):
    channel: Literal[Channel.TELEGRAM] = Channel.TELEGRAM
    message_id: uuid.UUID
    attempt: Annotated[int, Field(ge=1)]
    #: Config key for the bot; the adapter resolves the token from secret config.
    bot: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    chat_id: int = Field(repr=False)
    text: str = Field(repr=False)
    expires_at: AwareDatetime | None = None


SendRequest = SmsSendRequest | TelegramSendRequest


class ProviderResult(_Frozen):
    """Normalized result of exactly one provider call.

    ``detail`` and ``provider_status`` must never contain secrets, tokens, request or
    response bodies, message text or recipient identifiers.
    """

    outcome: ProviderOutcome
    provider: Provider
    #: 8028 ``sms_id`` or Telegram ``message_id`` (as string). Required for SENT.
    provider_message_id: str | None = None
    #: Raw provider status token, e.g. ``http_200``, ``http_503``, ``tg_403``, ``timeout``.
    provider_status: Annotated[str, StringConstraints(max_length=64)]
    failure_code: FailureCode | None = None
    #: Only meaningful for NOT_SENT. False => configuration/auth problem, do not loop.
    retryable: bool = False
    retry_after_seconds: Annotated[int, Field(ge=0)] | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime
    detail: Annotated[str, StringConstraints(max_length=200)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ProviderResult:
        if self.outcome is ProviderOutcome.SENT:
            if not self.provider_message_id:
                raise ValueError("sent requires provider_message_id")
            if self.failure_code is not None:
                raise ValueError("sent must not carry failure_code")
        else:
            if self.failure_code is None:
                raise ValueError("non-sent outcomes require failure_code")
        if self.outcome is ProviderOutcome.UNKNOWN and self.retryable:
            raise ValueError("unknown outcomes are never retryable")
        if self.outcome is not ProviderOutcome.NOT_SENT and self.retry_after_seconds is not None:
            raise ValueError("retry_after_seconds is only valid for not_sent")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        return self


@runtime_checkable
class MessageAdapter(Protocol):
    """One provider transport. Implementations: ``adapters/sms8028.py``, ``adapters/telegram.py``.

    Rules:
    * ``send`` performs at most one provider call and never retries internally.
    * Provider/network failures are returned as ``ProviderResult``; they are not raised.
      Any exception that escapes ``send`` is treated by the worker as ``UNKNOWN`` /
      ``INTERRUPTED`` because the call may already have reached the provider.
    * Never log or raise with secrets, bodies, message text or full recipients.
    """

    provider: ClassVar[Provider]
    channel: ClassVar[Channel]

    async def send(self, request: SendRequest) -> ProviderResult: ...


# --------------------------------------------------------------------------------------
# Delivery receipts — 8028 only (Telegram has no receipt integration in this scope)
# --------------------------------------------------------------------------------------


class ReceiptStatus(StrEnum):
    """8028 callback ``status`` values. Guide v1.1 (2026-11-12) documents ``Pending``,
    ``Delivered`` and ``Rejected``; ``Sent`` and ``Expired`` are kept from earlier
    versions. Anything else is recorded without a status change."""

    PENDING = "Pending"
    SENT = "Sent"
    DELIVERED = "Delivered"
    EXPIRED = "Expired"
    REJECTED = "Rejected"


#: Mapping of documented 8028 statuses to (message status, failure code, final).
#: ``Failed`` appears in the guide's sample code but not its status table: treated as
#: undocumented -> non-final, recorded, no status change (see docs/8028-contract.md).
#: The raw status is always preserved alongside the mapped code.
RECEIPT_STATUS_MAP: Mapping[ReceiptStatus, tuple[MessageStatus, FailureCode | None, bool]] = {
    # Pending: accepted by 8028, not yet delivered. Evidence the message left OAN, so an
    # ``unknown`` message becomes ``sent``; never overwrites a final status.
    ReceiptStatus.PENDING: (MessageStatus.SENT, None, False),
    ReceiptStatus.SENT: (MessageStatus.SENT, None, False),
    ReceiptStatus.DELIVERED: (MessageStatus.DELIVERED, None, True),
    ReceiptStatus.EXPIRED: (MessageStatus.FAILED, FailureCode.PROVIDER_DELIVERY_EXPIRED, True),
    ReceiptStatus.REJECTED: (MessageStatus.FAILED, FailureCode.PROVIDER_REJECTED, True),
}


class DeliveryReceipt(_Frozen):
    """An authenticated 8028 callback after parsing — **in memory only**.

    ``recipient`` is never persisted. Storage keeps a ``StoredReceipt`` with a keyed
    recipient fingerprint and the masked number instead.
    """

    provider: Literal[Provider.SMS_8028] = Provider.SMS_8028
    provider_message_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    #: E.164 recipient from the callback; matched with provider_message_id (both must agree).
    recipient: E164EthiopianMobile
    #: Raw value exactly as received, including undocumented values.
    raw_status: Annotated[str, StringConstraints(max_length=64)]
    #: Event time converted from EAT to an aware datetime.
    event_time: AwareDatetime
    received_at: AwareDatetime

    @property
    def mapped(self) -> tuple[MessageStatus, FailureCode | None, bool] | None:
        """None for undocumented statuses: record, log, do not change message status."""
        try:
            return RECEIPT_STATUS_MAP[ReceiptStatus(self.raw_status)]
        except ValueError:
            return None


class ReceiptDisposition(StrEnum):
    APPLIED = "applied"  # matched and moved status
    RECORDED_NO_CHANGE = "recorded_no_change"  # matched; duplicate, out-of-order or unmapped
    ORPHANED = "orphaned"  # no matching sms_id yet (callback-before-send-response); kept
    MISMATCHED = "mismatched"  # sms_id known but recipient differs: security event, no change
    UNMATCHED_LATE = "unmatched_late"  # after receipt_window; recorded, never applied


class StoredReceipt(_Frozen):
    """What storage persists for a callback. No raw phone number."""

    id: uuid.UUID
    provider: Literal[Provider.SMS_8028] = Provider.SMS_8028
    provider_message_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    #: Matched row's key version, or the current version for orphans.
    recipient_fingerprint: KeyedFingerprint
    recipient_masked: str
    raw_status: Annotated[str, StringConstraints(max_length=64)]
    event_time: AwareDatetime
    received_at: AwareDatetime
    disposition: ReceiptDisposition
    message_id: uuid.UUID | None = None


# --------------------------------------------------------------------------------------
# Persistence interface (implemented by MSG-02 storage, used by API, worker, receipts)
# --------------------------------------------------------------------------------------


class IdempotencyConflict(Exception):
    """Same (caller, channel, Idempotency-Key) with a different payload -> HTTP 409."""


class LeaseLost(Exception):
    """The claim token no longer owns the message (lease expired / reclaimed)."""


class StoredMessage(_Frozen):
    """Durable metadata. Content (text, recipient/chat ciphertext) lives in a separate
    encrypted column/table and is not part of this model."""

    id: uuid.UUID
    caller_id: str
    channel: Channel
    #: Kept for display/correlation for the row's lifetime. Unique per (caller_id, channel)
    #: among rows whose binding is live (partial unique index WHERE binding_released_at IS
    #: NULL). Callers must never recycle a key, even after release.
    idempotency_key: IdempotencyKey
    purpose: Purpose
    priority: int
    source_reference: SourceReference
    status: MessageStatus
    failure_code: FailureCode | None = None
    provider_status: str | None = None
    provider_message_id: str | None = None
    #: Required while the binding is live (survives content purge); None only after
    #: release. A live binding without a fingerprint is invalid, never "new".
    payload_fingerprint: KeyedFingerprint | None
    #: Set when ``purge_due`` released the binding (only completed messages, after
    #: ``dedup_window``). The row and GET remain available afterwards.
    binding_released_at: AwareDatetime | None = None
    #: SMS only. Required while accepted/sending; released at ``recipient_match_deadline``.
    recipient_fingerprint: KeyedFingerprint | None = None
    recipient_masked: str
    attempts: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    delivered_at: AwareDatetime | None = None
    #: Persisted time of the latest attempt (``begin_attempt``); anchors receipt matching
    #: for attempts that never produced ``sent_at`` (unknown, rejected, interrupted).
    last_attempt_at: AwareDatetime | None = None
    #: Earliest time the worker may claim it (retry backoff). None = now.
    not_before: AwareDatetime | None = None
    content_purged_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> StoredMessage:
        if self.binding_released_at is None:
            if self.payload_fingerprint is None:
                raise ValueError("a live idempotency binding requires payload_fingerprint")
            if self.payload_fingerprint.domain is not FingerprintDomain.PAYLOAD:
                raise ValueError("payload_fingerprint has the wrong domain")
        else:
            if self.payload_fingerprint is not None:
                raise ValueError("a released binding must not keep payload_fingerprint")
            if self.status in OUTSTANDING_BINDING_STATUSES:
                raise ValueError("bindings of accepted/sending/unknown messages are never released")
        if (self.attempts > 0) != (self.last_attempt_at is not None):
            raise ValueError("last_attempt_at must be set exactly when attempts > 0")
        if self.status is MessageStatus.SENT and self.sent_at is None:
            raise ValueError("sent requires sent_at")
        if (
            self.channel is Channel.SMS
            and self.status in CONTENT_REQUIRED_STATUSES
            and self.recipient_fingerprint is None
        ):
            raise ValueError("SMS recipient fingerprint is required while a send is possible")
        if self.recipient_fingerprint is not None:
            if self.channel is not Channel.SMS:
                raise ValueError("recipient_fingerprint is only kept for SMS")
            if self.recipient_fingerprint.domain is not FingerprintDomain.RECIPIENT:
                raise ValueError("recipient_fingerprint has the wrong domain")
        if self.status in CONTENT_REQUIRED_STATUSES and self.content_purged_at is not None:
            raise ValueError("content cannot be purged while a send is still possible")
        return self


def to_status_view(message: StoredMessage) -> MessageStatusView:
    """GET representation. Works for purged rows and rows whose binding was released."""
    return MessageStatusView(
        id=message.id,
        idempotency_key=message.idempotency_key,
        channel=message.channel,
        purpose=message.purpose,
        source_reference=message.source_reference,
        status=message.status,
        final=message.status in FINAL_STATUSES,
        failure_code=message.failure_code,
        provider_status=message.provider_status,
        recipient_masked=message.recipient_masked,
        delivery_receipts_supported=CHANNEL_DELIVERY_RECEIPTS[message.channel],
        attempts=message.attempts,
        created_at=message.created_at,
        updated_at=message.updated_at,
        expires_at=message.expires_at,
        sent_at=message.sent_at,
        delivered_at=message.delivered_at,
    )


class Claim(_Frozen):
    """A leased message ready for one attempt. ``send_request`` holds decrypted content."""

    message: StoredMessage
    claim_token: uuid.UUID
    lease_expires_at: AwareDatetime
    send_request: SendRequest


class MessageRepository(Protocol):
    """Durable store. All methods are transactional; all times are passed in (fake clock).

    Content purge rule: any transaction that moves a message into ``PURGEABLE_STATUSES``
    deletes its content in the same transaction. ``purge_due`` is the crash-safe backstop.
    """

    async def create_or_get(
        self,
        caller: CallerContext,
        idempotency_key: str,
        submission: Submission,
        now: datetime,
    ) -> tuple[StoredMessage, IdempotencyDecision]:
        """Look up the *live* binding for (caller_id, channel, idempotency_key) FIRST and
        classify with ``classify_idempotent_request`` (no age-based release here), then:

        * REPLAY -> return the original message (even if purged / expired); 200.
        * CONFLICT -> raise ``IdempotencyConflict``; 409.
        * NEW -> ``check_expiry``, compute fingerprints (current key version), persist
          metadata + encrypted content, commit before returning; 202.
        Concurrent identical requests must yield exactly one row (unique constraint);
        the loser re-reads and classifies.
        """
        ...

    async def get_for_caller(
        self, caller_id: str, message_id: uuid.UUID
    ) -> StoredMessage | None:
        """None for missing *and* other callers' messages (no existence oracle)."""
        ...

    async def claim_due(
        self, worker_id: str, now: datetime, lease: timedelta, limit: int,
        *, channels: tuple[Channel, ...] | None = None, allow_broadcast: bool = True,
        aged_after: timedelta | None = None,
    ) -> list[Claim]:
        """Lease up to ``limit`` ACCEPTED, due messages by (priority, created_at).

        With ``aged_after`` (SMS-03 broadcast ageing): OTPs first, then every other
        message accepted at least that long ago, oldest first, then the rest by
        (priority, created_at). A waiting broadcast is never starved by newer traffic.

        Must use row locks that let concurrent workers proceed (SKIP LOCKED).
        Expired messages are moved to EXPIRED (and purged) instead of being claimed.
        """
        ...

    async def begin_attempt(self, claim: Claim, now: datetime) -> int:
        """Commit ACCEPTED->SENDING, an attempt row and ``last_attempt_at`` *before* the
        network call.

        Returns the attempt number. Raises ``LeaseLost`` or rejects if the message has
        expired (caller re-checks expiry immediately before calling this).
        """
        ...

    async def finish_attempt(
        self, claim: Claim, result: ProviderResult, next_not_before: datetime | None
    ) -> StoredMessage:
        """Apply ``OUTCOME_TO_STATUS`` and store provider id/status. Raises ``LeaseLost``.

        In one transaction: store provider_message_id, re-match orphan receipts for it
        (content is still available here, so the recipient can be fingerprinted under
        the orphan's key version), then purge content if the new status is purgeable.
        """
        ...

    async def mark_expired(self, claim: Claim, now: datetime) -> StoredMessage:
        """-> EXPIRED / expired_before_send, purging content in the same transaction."""
        ...

    async def release_claim(self, claim: Claim, now: datetime) -> bool:
        """Clear the lease of a claim that never began an attempt (still ACCEPTED and
        still ours), so the message is claimable again at once. False if not ours."""
        ...

    async def recover_stale(self, now: datetime) -> int:
        """SENDING rows whose lease expired -> UNKNOWN / INTERRUPTED, purged. Returns count."""
        ...

    async def record_receipt(
        self, receipt: DeliveryReceipt, now: datetime
    ) -> ReceiptDisposition:
        """Persist a ``StoredReceipt`` (always) and apply it if matched and legal.

        Match requires provider_message_id AND recipient fingerprint equality under the
        message row's key version, before ``recipient_match_deadline``. Works after
        content purge. Must commit before the callback endpoint acknowledges. The raw
        ``receipt.recipient`` is never written.
        """
        ...

    async def purge_due(self, now: datetime, limit: int) -> int:
        """Backstop sweeper (every ``purge_sweep_interval``; resumes after outages):

        * delete content of messages in ``PURGEABLE_STATUSES`` that still have it;
        * release recipient fingerprints past ``recipient_match_deadline``;
        * release bindings only where ``binding_release_due`` (completed + window):
          set ``binding_released_at``, null ``payload_fingerprint``; keep the row;
        * delete receipt evidence past ``receipt_evidence_retention``.
        Never touches bindings of accepted/sending/unknown messages. Idempotent.
        Returns rows touched."""
        ...

    async def fingerprint_key_versions_in_use(self, now: datetime) -> frozenset[str]:
        """Key versions referenced by any retained fingerprint: live-binding payload
        fingerprints, unreleased recipient fingerprints, stored receipt fingerprints.
        Input to ``can_retire_key_version``."""
        ...


# --------------------------------------------------------------------------------------
# Examples and self-check (no network)
# --------------------------------------------------------------------------------------

_T0 = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)  # 08:12:13 EAT
_ID = uuid.UUID("0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10")
_KEY = "6f1d2c3e-8a4b-4c5d-9e6f-7a8b9c0d1e2f"

EXAMPLES: dict[str, tuple[type[BaseModel], dict[str, Any]]] = {
    "sms_otp_submission": (
        SmsSubmission,
        {
            "recipient": "+251912000001",
            "text": "Your OAN code is 482913. It expires in 5 minutes.",
            "source_reference": "esignet:otp:7f3c9a",
            "purpose": "otp",
            "expires_at": "2026-09-01T05:17:13Z",
        },
    ),
    "sms_amharic_submission": (
        SmsSubmission,
        {
            "recipient": "0712000002",
            "text": "እንደምን አደርሩ! የእርሻ ምክር ለማግኘት 8028 ይደውሉ።",
            "source_reference": "advisory:2026-09-01:w12",
            "purpose": "broadcast",
        },
    ),
    "telegram_submission": (
        TelegramSubmission,
        {
            "chat_id": 123456789,
            "text": "Your grievance G-1042 was updated.",
            "source_reference": "grievance:G-1042:update:3",
            "purpose": "transactional",
        },
    ),
    "accepted_response": (
        SubmissionResponse,
        {
            "id": str(_ID),
            "idempotency_key": _KEY,
            "channel": "sms",
            "status": "accepted",
            "source_reference": "esignet:otp:7f3c9a",
            "created_at": "2026-09-01T05:12:13Z",
            "expires_at": "2026-09-01T05:17:13Z",
        },
    ),
}

_SMS_VIEW: dict[str, Any] = {
    "id": str(_ID), "idempotency_key": _KEY, "channel": "sms", "purpose": "otp",
    "source_reference": "esignet:otp:7f3c9a", "recipient_masked": "+2519******01",
    "delivery_receipts_supported": True, "created_at": "2026-09-01T05:12:13Z",
    "expires_at": "2026-09-01T05:17:13Z",
}
_TG_VIEW: dict[str, Any] = {
    "id": "5d1e7c0a-2b4f-4a8e-8c61-0e9b7d3f2a11",
    "idempotency_key": "b3e8c1d4-2f6a-4b7c-8d9e-0a1b2c3d4e5f",
    "channel": "telegram", "purpose": "transactional",
    "source_reference": "grievance:G-1042:update:3", "recipient_masked": "chat:*******89",
    "delivery_receipts_supported": False, "created_at": "2026-09-01T05:12:13Z",
}
EXAMPLES.update(
    {
        "status_accepted": (MessageStatusView, {
            **_SMS_VIEW, "status": "accepted", "final": False, "attempts": 0,
            "updated_at": "2026-09-01T05:12:13Z"}),
        "status_sent": (MessageStatusView, {
            **_SMS_VIEW, "status": "sent", "final": False, "provider_status": "http_200",
            "attempts": 1, "updated_at": "2026-09-01T05:12:14Z",
            "sent_at": "2026-09-01T05:12:14Z"}),
        "status_delivered": (MessageStatusView, {
            **_SMS_VIEW, "status": "delivered", "final": True, "provider_status": "Delivered",
            "attempts": 1, "updated_at": "2026-09-01T05:12:19Z",
            "sent_at": "2026-09-01T05:12:14Z", "delivered_at": "2026-09-01T05:12:19Z"}),
        "status_unknown": (MessageStatusView, {
            **_SMS_VIEW, "status": "unknown", "final": False, "failure_code": "timeout",
            "provider_status": "timeout", "attempts": 1,
            "updated_at": "2026-09-01T05:12:29Z"}),
        "status_gateway_expired": (MessageStatusView, {
            **_SMS_VIEW, "status": "failed", "final": True,
            "failure_code": "provider_delivery_expired", "provider_status": "Expired",
            "attempts": 1, "updated_at": "2026-09-02T05:12:13Z",
            "sent_at": "2026-09-01T05:12:14Z"}),
        "status_telegram_sent": (MessageStatusView, {
            **_TG_VIEW, "status": "sent", "final": False, "provider_status": "tg_200",
            "attempts": 1, "updated_at": "2026-09-01T05:12:14Z",
            "sent_at": "2026-09-01T05:12:14Z"}),
        "result_sent": (ProviderResult, {
            "outcome": "sent", "provider": "sms8028", "provider_message_id": "17006100",
            "provider_status": "http_200", "started_at": "2026-09-01T05:12:13Z",
            "finished_at": "2026-09-01T05:12:14Z"}),
        "result_rejected": (ProviderResult, {
            "outcome": "rejected", "provider": "sms8028", "provider_status": "http_400",
            "failure_code": "invalid_recipient", "started_at": "2026-09-01T05:12:13Z",
            "finished_at": "2026-09-01T05:12:14Z"}),
        "result_not_sent_429": (ProviderResult, {
            "outcome": "not_sent", "provider": "telegram", "provider_status": "tg_429",
            "failure_code": "rate_limited", "retryable": True, "retry_after_seconds": 17,
            "started_at": "2026-09-01T05:12:13Z", "finished_at": "2026-09-01T05:12:13Z"}),
        "result_unknown_timeout": (ProviderResult, {
            "outcome": "unknown", "provider": "sms8028", "provider_status": "timeout",
            "failure_code": "timeout", "started_at": "2026-09-01T05:12:13Z",
            "finished_at": "2026-09-01T05:12:28Z"}),
        "receipt_delivered": (DeliveryReceipt, {
            "provider_message_id": "17006100", "recipient": "+251912000001",
            "raw_status": "Delivered", "event_time": "2026-09-01T08:12:19+03:00",
            "received_at": "2026-09-01T05:12:20Z"}),
    }
)

#: Examples that MUST fail validation, with the reason they exist.
INVALID_EXAMPLES: dict[str, tuple[type[BaseModel], dict[str, Any]]] = {
    "otp_without_expiry": (
        SmsSubmission,
        {"recipient": "0912000001", "text": "Code 1", "source_reference": "x1", "purpose": "otp"},
    ),
    "invalid_number_9_digits": (
        SmsSubmission,
        {"recipient": "091200000", "text": "hi", "source_reference": "x2", "purpose": "alert"},
    ),
    "landline_number": (
        SmsSubmission,
        {"recipient": "0111000000", "text": "hi", "source_reference": "x3", "purpose": "alert"},
    ),
    "amharic_over_part_limit": (
        SmsSubmission,
        {"recipient": "0912000001", "text": "ሀ" * 202, "source_reference": "x4",
         "purpose": "broadcast"},
    ),
    "latin_over_part_limit": (
        SmsSubmission,
        {"recipient": "0912000001", "text": "a" * 460, "source_reference": "x5",
         "purpose": "broadcast"},
    ),
    "naive_expiry": (
        SmsSubmission,
        {"recipient": "0912000001", "text": "hi", "source_reference": "x6", "purpose": "otp",
         "expires_at": "2026-09-01T05:17:13"},
    ),
    "idempotency_key_in_body": (
        SmsSubmission,
        {"recipient": "0912000001", "text": "hi", "source_reference": "x6b", "purpose": "alert",
         "idempotency_key": "body-is-not-allowed"},
    ),
    "telegram_token_field": (
        TelegramSubmission,
        {"chat_id": 1, "text": "hi", "source_reference": "x7", "purpose": "alert",
         "bot_token": "123:ABC"},
    ),
    "telegram_string_chat_id": (
        TelegramSubmission,
        {"chat_id": "123", "text": "hi", "source_reference": "x8", "purpose": "alert"},
    ),
    "sent_without_provider_id": (
        ProviderResult,
        {"outcome": "sent", "provider": "sms8028", "provider_status": "http_200",
         "started_at": "2026-09-01T05:12:13Z", "finished_at": "2026-09-01T05:12:14Z"},
    ),
    "unknown_marked_retryable": (
        ProviderResult,
        {"outcome": "unknown", "provider": "sms8028", "provider_status": "http_503",
         "failure_code": "provider_server_error", "retryable": True,
         "started_at": "2026-09-01T05:12:13Z", "finished_at": "2026-09-01T05:12:14Z"},
    ),
    # Review probe: SMS delivered example relabelled as Telegram, flag left true.
    "telegram_delivered_flag_true": (
        MessageStatusView,
        {**_SMS_VIEW, "channel": "telegram", "status": "delivered", "final": True,
         "provider_status": "Delivered", "attempts": 1, "updated_at": "2026-09-01T05:12:19Z",
         "sent_at": "2026-09-01T05:12:14Z", "delivered_at": "2026-09-01T05:12:19Z"},
    ),
    "telegram_delivered_flag_false": (
        MessageStatusView,
        {**_TG_VIEW, "status": "delivered", "final": True, "attempts": 1,
         "updated_at": "2026-09-01T05:12:14Z", "delivered_at": "2026-09-01T05:12:14Z"},
    ),
    "telegram_sent_flag_true": (
        MessageStatusView,
        {**_TG_VIEW, "delivery_receipts_supported": True, "status": "sent", "final": False,
         "attempts": 1, "updated_at": "2026-09-01T05:12:14Z",
         "sent_at": "2026-09-01T05:12:14Z"},
    ),
    "sms_flag_false": (
        MessageStatusView,
        {**_SMS_VIEW, "delivery_receipts_supported": False, "status": "sent", "final": False,
         "attempts": 1, "updated_at": "2026-09-01T05:12:14Z",
         "sent_at": "2026-09-01T05:12:14Z"},
    ),
    "telegram_receipt": (
        DeliveryReceipt,
        {"provider": "telegram", "provider_message_id": "55", "recipient": "+251912000001",
         "raw_status": "Delivered", "event_time": "2026-09-01T08:12:19+03:00",
         "received_at": "2026-09-01T05:12:20Z"},
    ),
}

#: Invalid examples that the OpenAPI schema itself must also reject (not just the model).
SCHEMA_ENFORCED_INVALID: frozenset[str] = frozenset(
    {
        "otp_without_expiry",
        "invalid_number_9_digits",
        "landline_number",
        "latin_over_part_limit",
        "idempotency_key_in_body",
        "telegram_token_field",
        "telegram_string_chat_id",
        "telegram_delivered_flag_true",
        "telegram_delivered_flag_false",
        "telegram_sent_flag_true",
        "sms_flag_false",
    }
)


def _check(failures: list[str], ok: bool, label: str) -> None:
    if not ok:
        failures.append(label)


def _stored(
    fp: Fingerprinter, key: str, sub: Submission, caller: str, *, created: datetime,
) -> StoredMessage:
    rfp = (fp.fingerprint(FingerprintDomain.RECIPIENT, canonical_recipient(sub.recipient))
           if isinstance(sub, SmsSubmission) else None)
    return StoredMessage(
        id=uuid.uuid4(), caller_id=caller, channel=submission_channel(sub), idempotency_key=key,
        purpose=sub.purpose, priority=PURPOSE_PRIORITY[sub.purpose],
        source_reference=sub.source_reference, status=MessageStatus.ACCEPTED,
        payload_fingerprint=fp.fingerprint(FingerprintDomain.PAYLOAD,
                                           canonical_payload(caller, sub)),
        recipient_fingerprint=rfp,
        recipient_masked=(mask_recipient(sub.recipient) if isinstance(sub, SmsSubmission)
                          else mask_chat_id(sub.chat_id)),
        attempts=0, created_at=created, updated_at=created, expires_at=sub.expires_at,
    )


def _evolve(message: StoredMessage, **update: Any) -> StoredMessage:
    """Validated state change (model_copy would skip validators)."""
    return StoredMessage.model_validate(message.model_dump() | update)


def _rejects(failures: list[str], label: str, fn: Any) -> None:
    try:
        fn()
        failures.append(label)
    except ValueError:  # pydantic ValidationError subclasses ValueError
        pass


#: Opaque example keys (random UUIDs). Keys must never embed OTPs, phones or other PII.
_K_R1 = "1c9e4f7a-3b2d-4e8f-a6c5-9d0b1e2f3a4b"
_K_R2 = "8a7b6c5d-4e3f-4a1b-9c8d-7e6f5a4b3c2d"


def _check_idempotency_and_fingerprints(failures: list[str]) -> None:
    """Contract-level simulation of the MSG-02/05 rules (in memory, no database)."""
    fp = HmacFingerprinter({"k1": b"\x01" * 32, "k2": b"\x02" * 32}, "k1")
    pol = DEFAULT_POLICY
    caller = "esignet"
    event = "esignet:otp:7f3c9a"
    exp = _T0 + timedelta(minutes=5)
    a = SmsSubmission(recipient="0912000001", text="Code 482913", source_reference=event,
                      purpose=Purpose.OTP, expires_at=exp)
    b = SmsSubmission(recipient="0912000002", text="Code 482913", source_reference=event,
                      purpose=Purpose.OTP, expires_at=exp)

    def pfp(sub: Submission, version: str = "k1") -> KeyedFingerprint:
        return fp.fingerprint(FingerprintDomain.PAYLOAD, canonical_payload(caller, sub), version)

    # Two deliveries of one source event: distinct keys, both accepted.
    store: dict[tuple[str, Channel, str], StoredMessage] = {}
    for key, sub in [(_K_R1, a), (_K_R2, b)]:
        scope = (caller, Channel.SMS, key)
        d = classify_idempotent_request(store.get(scope), pfp(sub))
        _check(failures, d is IdempotencyDecision.NEW, f"shared event: {key} should be new")
        store[scope] = _stored(fp, key, sub, caller, created=_T0)
    _check(failures, len(store) == 2, "shared event: expected two messages")
    _check(failures, not pfp(a).matches(pfp(b)), "shared event: payload fingerprints collide")

    row_a = store[(caller, Channel.SMS, _K_R1)]
    same_a_other_format = a.model_copy(update={"recipient": normalize_et_mobile("+251912000001")})
    later = _T0 + timedelta(seconds=3)
    _check(failures, classify_idempotent_request(row_a, pfp(same_a_other_format))
           is IdempotencyDecision.REPLAY, "replay: identical payload not recognized")
    _check(failures, classify_idempotent_request(row_a, pfp(b))
           is IdempotencyDecision.CONFLICT, "conflict: different recipient under same key")

    # Lifecycle rows (validated transitions).
    sending = _evolve(row_a, status=MessageStatus.SENDING, attempts=1, last_attempt_at=later)
    sent = _evolve(sending, status=MessageStatus.SENT, sent_at=later, content_purged_at=later,
                   provider_message_id="17006100")
    unknown = _evolve(sending, status=MessageStatus.UNKNOWN, failure_code=FailureCode.TIMEOUT,
                      content_purged_at=later)
    rejected = _evolve(sending, status=MessageStatus.FAILED,
                       failure_code=FailureCode.INVALID_RECIPIENT, content_purged_at=later)
    delivered = _evolve(sent, status=MessageStatus.DELIVERED, delivered_at=later)
    expired = _evolve(row_a, status=MessageStatus.EXPIRED,
                      failure_code=FailureCode.EXPIRED_BEFORE_SEND, content_purged_at=exp)

    # After purge and after original expiry: replay works, a change conflicts.
    after_expiry = exp + timedelta(hours=1)
    _check(failures, classify_idempotent_request(sent, pfp(a)) is IdempotencyDecision.REPLAY,
           "replay after purge/expiry rejected")
    _rejects(failures, "new acceptance after expiry should fail check_expiry",
             lambda: check_expiry(a.purpose, a.expires_at, after_expiry))
    changed = a.model_copy(update={"text": "Code 999999"})
    _check(failures, classify_idempotent_request(sent, pfp(changed))
           is IdempotencyDecision.CONFLICT, "changed content after purge not detected")

    # P1: outstanding bindings are retained at and beyond the guarantee window.
    window_end = _T0 + pol.dedup_window
    for name, row in [("accepted", row_a), ("sending", sending), ("unknown", unknown)]:
        for t in (window_end, window_end + timedelta(days=365)):
            _check(failures, not binding_release_due(row, t, pol),
                   f"{name} binding released at {t - _T0}")
        _check(failures, classify_idempotent_request(row, pfp(a)) is IdempotencyDecision.REPLAY,
               f"{name} binding at 30 days did not replay")
        _check(failures, classify_idempotent_request(row, pfp(changed))
               is IdempotencyDecision.CONFLICT, f"{name} binding at 30 days did not conflict")
        _rejects(failures, f"{name} row with released binding accepted",
                 lambda row=row: _evolve(row, binding_released_at=window_end,
                                         payload_fingerprint=None))

    # Completed bindings: kept inside the window, releasable after it.
    for name, row in [("sent", sent), ("delivered", delivered), ("failed", rejected),
                      ("expired", expired)]:
        _check(failures, not binding_release_due(row, window_end - timedelta(seconds=1), pol),
               f"{name} binding released before window")
        _check(failures, binding_release_due(row, window_end, pol),
               f"{name} binding not releasable after window")
        released = _evolve(row, binding_released_at=window_end, payload_fingerprint=None)
        _check(failures, classify_idempotent_request(released, pfp(a))
               is IdempotencyDecision.NEW, f"{name} released binding not free")
        _check(failures, not binding_release_due(released, window_end, pol),
               f"{name} released twice")
        # P2: GET still works after release (and after purge).
        view = to_status_view(released)
        _check(failures, view.id == row.id and view.status is row.status
               and view.idempotency_key == _K_R1, f"{name} GET view after release wrong")
        _rejects(failures, f"{name} released binding kept a fingerprint",
                 lambda row=row: _evolve(row, binding_released_at=window_end))

    # P2: a live binding without a fingerprint is invalid, never NEW.
    _rejects(failures, "live binding without payload_fingerprint accepted",
             lambda: _evolve(sent, payload_fingerprint=None))
    _rejects(failures, "classify accepted a live binding without candidate",
             lambda: classify_idempotent_request(sent, None))

    # P2: receipt deadlines, including attempts without sent_at.
    rw = pol.receipt_window
    for name, row, anchor in [
        ("sent", sent, sent.sent_at),
        ("unknown (no sent_at)", unknown, unknown.last_attempt_at),
        ("rejected (no sent_at)", rejected, rejected.last_attempt_at),
        ("expired before send", expired, expired.created_at),
    ]:
        assert anchor is not None
        _check(failures, recipient_match_deadline(row, pol) == anchor + rw,
               f"receipt deadline wrong for {name}")
    for name, row in [("accepted", row_a), ("sending", sending)]:
        _check(failures, recipient_match_deadline(row, pol) is None,
               f"{name} recipient fingerprint must be kept")
    _check(failures, unknown.sent_at is None, "unknown fixture should lack sent_at")
    # Releasing the recipient fingerprint of an unknown message keeps it unknown and bound.
    unknown_late = _evolve(unknown, recipient_fingerprint=None)
    _check(failures, unknown_late.status is MessageStatus.UNKNOWN
           and not binding_release_due(unknown_late, window_end, pol)
           and MessageStatus.ACCEPTED not in ALLOWED_TRANSITIONS[MessageStatus.UNKNOWN],
           "unknown lost no-resend protection after fingerprint release")
    _rejects(failures, "SMS accepted row without recipient fingerprint accepted",
             lambda: _evolve(row_a, recipient_fingerprint=None))
    _rejects(failures, "attempts without last_attempt_at accepted",
             lambda: _evolve(row_a, attempts=1))
    _rejects(failures, "sent without sent_at accepted",
             lambda: _evolve(sending, status=MessageStatus.SENT, content_purged_at=later))
    tg = TelegramSubmission(chat_id=5, text="hi", source_reference="t", purpose=Purpose.ALERT)
    _check(failures, recipient_match_deadline(
        _stored(fp, _K_R2, tg, caller, created=_T0), pol) is None, "telegram has no receipts")

    # Key versions and retirement driven by live dependencies, not elapsed time.
    _check(failures, pfp(a, "k1").digest != pfp(a, "k2").digest, "key versions collide")
    _rejects(failures, "cross-version comparison was allowed",
             lambda: classify_idempotent_request(sent, pfp(a, "k2")))
    _rejects(failures, "unknown key version accepted",
             lambda: fp.fingerprint(FingerprintDomain.PAYLOAD, b"x", "retired"))
    _rejects(failures, "short fingerprint key accepted",
             lambda: keyed_fingerprint(b"short", "k0", FingerprintDomain.PAYLOAD, b"x"))
    in_use = frozenset({unknown.payload_fingerprint.key_version}
                       if unknown.payload_fingerprint else set())
    _check(failures, not can_retire_key_version("k1", "k2", in_use),
           "key retired while an unknown binding depends on it")
    _check(failures, can_retire_key_version("k1", "k2", frozenset()),
           "unused old key not retirable")
    _check(failures, not can_retire_key_version("k2", "k2", frozenset()),
           "current key retirable")

    # Domain separation; not a plain hash.
    _check(failures, fp.fingerprint(FingerprintDomain.PAYLOAD, b"+251912000001").digest
           != fp.fingerprint(FingerprintDomain.RECIPIENT, b"+251912000001").digest,
           "fingerprint domains not separated")
    _check(failures, fp.fingerprint(FingerprintDomain.RECIPIENT, b"+251912000001").digest
           != hashlib.sha256(b"+251912000001").hexdigest(), "fingerprint is a plain hash")

    # Receipt matching after purge, using only the retained recipient fingerprint.
    assert sent.recipient_fingerprint is not None
    for phone, expect in [("0912000001", True), ("+251912000001", True), ("0912000002", False)]:
        rec = DeliveryReceipt(provider_message_id="17006100", recipient=normalize_et_mobile(phone),
                              raw_status="Delivered", event_time=later, received_at=later)
        cand = fp.fingerprint(FingerprintDomain.RECIPIENT, canonical_recipient(rec.recipient),
                              sent.recipient_fingerprint.key_version)
        _check(failures, sent.recipient_fingerprint.matches(cand) is expect,
               f"receipt match after purge wrong for {phone}")
    _check(failures, "recipient" not in set(StoredReceipt.model_fields),
           "StoredReceipt keeps raw phone")

    # Purge invariants.
    _check(failures, MessageStatus.SENT in PURGEABLE_STATUSES, "sent must be purgeable")
    _check(failures, MessageStatus.UNKNOWN in PURGEABLE_STATUSES, "unknown must be purgeable")
    _check(failures, PURGEABLE_STATUSES.isdisjoint(CONTENT_REQUIRED_STATUSES), "purge overlap")
    _check(failures, MessageStatus.SENT in COMPLETED_BINDING_STATUSES
           and MessageStatus.UNKNOWN in OUTSTANDING_BINDING_STATUSES,
           "sent (no more sends) vs unknown (unproven) not distinguished")
    _rejects(failures, "purged content allowed while accepted",
             lambda: _evolve(row_a, content_purged_at=later))
    tg_row = _stored(fp, _K_R2, tg, caller, created=_T0).model_dump()
    _rejects(failures, "telegram row accepted a recipient fingerprint",
             lambda: StoredMessage.model_validate(
                 tg_row | {"recipient_fingerprint": sent.recipient_fingerprint}))

    # Policy: coherent and strictly positive.
    _check(failures, pol.content_retention_target_after_send == timedelta(minutes=7),
           "content retention target changed")
    for bad in [{"dedup_window": timedelta(days=7)}, {"max_otp_ttl": timedelta(days=8)},
                {"receipt_evidence_retention": timedelta(days=1)}]:
        _rejects(failures, f"incoherent policy accepted: {sorted(bad)}",
                 lambda bad=bad: ServicePolicy(**bad))
    for name in ServicePolicy.model_fields:
        for value in (timedelta(0), timedelta(days=-1)):
            _rejects(failures, f"non-positive policy {name}={value} accepted",
                     lambda name=name, value=value: ServicePolicy(**{name: value}))
    custom = ServicePolicy(max_otp_ttl=timedelta(minutes=10))
    _rejects(failures, "custom OTP ceiling ignored",
             lambda: check_expiry(Purpose.OTP, _T0 + timedelta(minutes=15), _T0, custom))

    # Opaque example keys: every example key is a UUID (no OTP/phone/PII).
    keys = [_K_R1, _K_R2] + [d["idempotency_key"] for _, d in EXAMPLES.values()
                             if "idempotency_key" in d]
    for k in keys:
        try:
            uuid.UUID(k)
        except ValueError:
            failures.append(f"example idempotency key is not opaque: {k}")


def _self_check(openapi_path: str | None = None) -> list[str]:
    from pydantic import ValidationError

    failures: list[str] = []
    for name, (model, data) in EXAMPLES.items():
        try:
            model.model_validate(data)
        except ValidationError as exc:
            failures.append(f"valid example {name} rejected: {exc.errors()}")
    for name, (model, data) in INVALID_EXAMPLES.items():
        try:
            model.model_validate(data)
            failures.append(f"invalid example {name} was accepted")
        except ValidationError:
            pass

    # Lifecycle invariants.
    for final in FINAL_STATUSES:
        _check(failures, not ALLOWED_TRANSITIONS[final], f"final {final} has transitions")
    _check(failures, MessageStatus.ACCEPTED not in ALLOWED_TRANSITIONS[MessageStatus.UNKNOWN],
           "unknown must never return to accepted (blind resend)")
    _check(failures, set(ALLOWED_TRANSITIONS) == set(MessageStatus), "transition table gaps")
    for target in OUTCOME_TO_STATUS.values():
        _check(failures, is_allowed_transition(MessageStatus.SENDING, target),
               f"outcome target {target} not reachable from sending")
    for mapped_status, _, _ in RECEIPT_STATUS_MAP.values():
        _check(failures, mapped_status is MessageStatus.SENT
               or is_allowed_transition(MessageStatus.SENT, mapped_status),
               f"receipt target {mapped_status} not reachable from sent")
    _check(failures, PURPOSE_PRIORITY[Purpose.OTP] < PURPOSE_PRIORITY[Purpose.BROADCAST],
           "otp must outrank broadcast")

    # Expiry rule with a fixed clock (new acceptances).
    for purpose, exp, ok in [
        (Purpose.OTP, None, False),
        (Purpose.OTP, _T0 + timedelta(minutes=5), True),
        (Purpose.OTP, _T0 - timedelta(seconds=1), False),
        (Purpose.OTP, _T0 + timedelta(hours=1), False),
        (Purpose.BROADCAST, None, True),
        (Purpose.BROADCAST, _T0 + timedelta(days=8), False),
    ]:
        try:
            check_expiry(purpose, exp, _T0)
            _check(failures, ok, f"check_expiry accepted {purpose} {exp}")
        except ValueError:
            _check(failures, not ok, f"check_expiry rejected {purpose} {exp}")

    # Normalization and masking.
    for raw in ("0912000001", "+251912000001", "251912000001"):
        _check(failures, normalize_et_mobile(raw) == "+251912000001", f"normalize {raw}")
    _check(failures, mask_recipient("+251912000001") == "+2519******01", "mask_recipient")
    _check(failures, mask_chat_id(123456789) == "chat:*******89", "mask_chat_id")

    # Length boundaries.
    # One part 160 / 70; three parts 3 x 153 = 459 septets, 3 x 67 = 201 units.
    # A 2-septet extension character or a surrogate pair is never split across parts.
    for text, ok in [("a" * 459, True), ("a" * 460, False), ("{" * 80, True),
                     ("{" * 228 + "a", True), ("{" * 228 + "aa", False), ("ሀ" * 201, True),
                     ("ሀ" * 202, False), ("a" * 199 + "😀", True),
                     ("ሀ" * 66 + "😀" + "ሀ" * 133, False)]:
        try:
            SmsSubmission(recipient="0912000001", text=text, source_reference="b",
                          purpose=Purpose.ALERT)
            _check(failures, ok, f"sms length accepted len={len(text)}")
        except ValidationError:
            _check(failures, not ok, f"sms length rejected len={len(text)}")
    for text, ok in [("a" * 160, True), ("a" * 161, False), ("ሀ" * 70, True), ("ሀ" * 71, False)]:
        try:
            SmsSubmission(recipient="0912000001", text=text, source_reference="b",
                          purpose=Purpose.OTP, expires_at=_T0 + timedelta(minutes=5))
            _check(failures, ok, f"otp length accepted len={len(text)}")
        except ValidationError:
            _check(failures, not ok, f"otp length rejected len={len(text)}")
    _check(failures, [sms_parts(t) for t in ("", "a" * 160, "a" * 161, "a" * 306, "a" * 307,
                                             "ሀ" * 70, "ሀ" * 71, "ሀ" * 134, "ሀ" * 135)]
           == [1, 1, 2, 2, 3, 1, 2, 2, 3], "sms_parts boundaries")

    # Undocumented receipt status is non-final and maps to None; Expired keeps raw status.
    base = EXAMPLES["receipt_delivered"][1]
    _check(failures, DeliveryReceipt.model_validate({**base, "raw_status": "Failed"}).mapped
           is None, "undocumented receipt status must not map")
    exp_r = DeliveryReceipt.model_validate({**base, "raw_status": "Expired"})
    _check(failures, exp_r.mapped == (MessageStatus.FAILED,
                                      FailureCode.PROVIDER_DELIVERY_EXPIRED, True)
           and exp_r.raw_status == "Expired", "gateway Expired mapping")

    _check_idempotency_and_fingerprints(failures)

    if openapi_path:
        failures.extend(_check_openapi(openapi_path))
    return failures


_OPENAPI_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "SmsSubmission": SmsSubmission,
    "TelegramSubmission": TelegramSubmission,
    "SubmissionResponse": SubmissionResponse,
    "MessageStatusView": MessageStatusView,
    "Problem": Problem,
}


def _check_openapi(path: str) -> list[str]:
    """Validate the spec, then validate every example against BOTH the OpenAPI schema
    and the matching pydantic model, and compare property/enum/header contracts."""
    import jsonschema
    import yaml
    from openapi_spec_validator import validate
    from pydantic import ValidationError

    failures: list[str] = []
    with open(path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    validate(spec)
    schemas = spec["components"]["schemas"]
    defs = _rewrite_refs(schemas)

    def schema_accepts(schema: dict[str, Any], value: Any) -> bool:
        try:
            jsonschema.validate(value, {"$defs": defs, **_rewrite_refs(schema)},
                                format_checker=jsonschema.FormatChecker())
            return True
        except jsonschema.ValidationError:
            return False

    post_ops = []
    for op_path, item in spec["paths"].items():
        for method, op in item.items():
            if method not in {"get", "post"}:
                continue
            if method == "post":
                post_ops.append((op_path, op))
            targets: list[tuple[dict[str, Any], dict[str, Any]]] = []
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                targets.append((body["schema"], body.get("examples", {})))
            for resp in op["responses"].values():
                if "$ref" in resp:
                    resp = spec["components"]["responses"][resp["$ref"].rsplit("/", 1)[-1]]
                for media in resp.get("content", {}).values():
                    targets.append((media["schema"], media.get("examples", {})))
            for schema, examples in targets:
                ref_name = schema.get("$ref", "").rsplit("/", 1)[-1]
                model = _OPENAPI_SCHEMA_MODELS.get(ref_name)
                for ex_name, ex in examples.items():
                    value = ex["value"]
                    expect_valid = not ex_name.startswith("invalid_")
                    label = f"{method.upper()} {op_path} example {ex_name}"
                    schema_ok = schema_accepts(schema, value)
                    model_ok = True
                    if model is not None:
                        try:
                            model.model_validate(value)
                        except ValidationError:
                            model_ok = False
                    if expect_valid and not (schema_ok and model_ok):
                        failures.append(f"{label}: schema_ok={schema_ok} model_ok={model_ok}")
                    if not expect_valid and model_ok:
                        failures.append(f"{label}: invalid example accepted by model")

    # Every valid model example of an OpenAPI schema is accepted by the schema; listed
    # invalid examples are rejected by the schema too.
    model_to_schema = {m: n for n, m in _OPENAPI_SCHEMA_MODELS.items()}
    for name, (model, data) in EXAMPLES.items():
        if model in model_to_schema and not schema_accepts(
            {"$ref": f"#/components/schemas/{model_to_schema[model]}"}, data
        ):
            failures.append(f"openapi schema rejects valid example {name}")
    for name in SCHEMA_ENFORCED_INVALID:
        model, data = INVALID_EXAMPLES[name]
        if schema_accepts({"$ref": f"#/components/schemas/{model_to_schema[model]}"}, data):
            failures.append(f"openapi schema accepts invalid example {name}")

    # Idempotency-Key header: required on every POST, same pattern as the contract.
    params = spec["components"]["parameters"]
    for op_path, op in post_ops:
        names = [params[p["$ref"].rsplit("/", 1)[-1]] if "$ref" in p else p
                 for p in op.get("parameters", [])]
        hdr = [p for p in names if p["in"] == "header" and p["name"] == "Idempotency-Key"]
        if not hdr or not hdr[0].get("required"):
            failures.append(f"POST {op_path}: Idempotency-Key header missing or optional")
        elif hdr[0]["schema"].get("pattern") != IDEMPOTENCY_KEY_PATTERN:
            failures.append(f"POST {op_path}: Idempotency-Key pattern differs from contract")
    if schemas["SourceReference"].get("pattern") != SOURCE_REFERENCE_PATTERN:
        failures.append("SourceReference pattern differs from contract")

    # Structural parity: properties, required, enums.
    for name, model in _OPENAPI_SCHEMA_MODELS.items():
        oa = schemas[name]
        model_props = set(model.model_fields)
        oa_props = set(oa.get("properties", {}))
        if model_props != oa_props:
            failures.append(f"{name}: property mismatch model={model_props} openapi={oa_props}")
        model_required = {n for n, f in model.model_fields.items() if f.is_required()}
        if model_required != set(oa.get("required", [])):
            failures.append(f"{name}: required mismatch")
    for enum_name, enum_cls in [("MessageStatus", MessageStatus), ("Purpose", Purpose),
                                ("Channel", Channel), ("FailureCode", FailureCode)]:
        if set(schemas[enum_name]["enum"]) != {e.value for e in enum_cls}:
            failures.append(f"{enum_name}: enum mismatch")
    # Opaque keys in every OpenAPI example (header examples and echoed idempotency_key).
    def walk(node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "idempotency_key" and isinstance(v, str):
                    found.append(v)
                found.extend(walk(v))
        elif isinstance(node, list):
            for v in node:
                found.extend(walk(v))
        return found

    header_keys = [ex["value"] for ex in params["IdempotencyKey"].get("examples", {}).values()]
    for k in header_keys + walk(spec["paths"]) + walk(spec["components"]["responses"]):
        if k == "body-is-not-allowed":  # deliberate invalid body example
            continue
        try:
            uuid.UUID(k)
        except ValueError:
            failures.append(f"openapi example idempotency key is not opaque: {k}")
    if spec["info"]["version"] != CONTRACT_VERSION:
        failures.append("openapi info.version differs from CONTRACT_VERSION")
    return failures


def _rewrite_refs(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: (v.replace("#/components/schemas/", "#/$defs/") if k == "$ref" else _rewrite_refs(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(v) for v in node]
    return node


if __name__ == "__main__":  # pragma: no cover
    import pathlib
    import sys

    spec_path = pathlib.Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"
    problems = _self_check(str(spec_path) if spec_path.exists() else None)
    for p in problems:
        print("FAIL", p)
    print(
        f"contract {CONTRACT_VERSION}: {len(EXAMPLES)} valid + {len(INVALID_EXAMPLES)} invalid "
        f"examples ({len(SCHEMA_ENFORCED_INVALID)} schema-enforced), idempotency/fingerprint/"
        f"purge simulation, openapi={'checked' if spec_path.exists() else 'missing'}, "
        f"failures={len(problems)}"
    )
    sys.exit(1 if problems else 0)
