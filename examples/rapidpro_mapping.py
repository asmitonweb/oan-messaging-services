"""PROPOSED RapidPro bridge mapping (see docs/integrations/rapidpro.md).

NOT A CONNECTOR AND NOT INSTALLED. This is the pure mapping a future bridge would apply
to a courier External-channel request (``send_body`` JSON template with ``{{uuid}}``,
``{{to}}``, ``{{text}}``, ``{{channel}}``; verified against courier v26.3.82) before
calling OAN with ``oan_client``.

* ``Idempotency-Key = rapidpro:<message uuid>``. Courier retries of the same message
  become OAN same-key replays (200, same id), never a second SMS.
* ``source_reference = rapidpro:<message uuid>`` for correlation.
* The text must fit ONE SMS (pilot); over-long text is refused here, never split.

Splitting is NOT solved by this mapping. Courier splits by UTF-8 bytes BEFORE the bridge
sees anything, and every fragment carries the same UUID. A first fragment that fits one
SMS is indistinguishable from a complete message, so this mapping would accept it and OAN
would send a truncated message; any later fragment (same key, different text) is refused
with 409. Neither the UUID nor the text lets the mapping reconstruct or reliably detect
fragments. Raising ``max_length`` (e.g. to 640) protects every valid pilot message
but does not stop longer input from being split. A future bridge therefore requires one
of these PREREQUISITES (neither is verified or implemented in MSG-06):

1. validation before courier splits: the flow/campaign/API path refuses text that does not
   fit one SMS, so courier never receives it; or
2. a demonstrably non-splitting configuration: a hard upstream bound of N characters on
   message text plus ``max_length >= 4 * N`` bytes (``is_non_splitting``), verified against
   the deployed RapidPro/mailroom version.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from oan_client import SmsDelivery

GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION = frozenset("^{}\\[~]|€\f")
_E164_ET = re.compile(r"^\+251[79][0-9]{8}$")


def fits_one_sms(text: str) -> bool:
    """Same rule OAN enforces: 160 GSM-7 septets (extension chars = 2), else 70 UTF-16."""
    if all(c in GSM7_BASIC or c in GSM7_EXTENSION for c in text):
        return sum(2 if c in GSM7_EXTENSION else 1 for c in text) <= 160
    return len(text.encode("utf-16-le")) // 2 <= 70


def courier_would_split(text: str, max_length: int) -> bool:
    """courier ``SplitText`` splits when ``len(text)`` in UTF-8 BYTES exceeds max_length."""
    return len(text.encode("utf-8")) > max_length


#: UTF-8 encodes one code point in at most 4 bytes.
UTF8_MAX_BYTES_PER_CHAR = 4


def is_non_splitting(max_length_bytes: int, hard_input_bound_chars: int) -> bool:
    """True only if courier cannot split ANY text of at most ``hard_input_bound_chars``
    code points. The bound must be enforced upstream (RapidPro/mailroom); courier itself
    enforces none. E.g. a 160-character bound needs ``max_length >= 640``."""
    if max_length_bytes <= 0 or hard_input_bound_chars <= 0:
        raise ValueError("bounds must be positive")
    return max_length_bytes >= UTF8_MAX_BYTES_PER_CHAR * hard_input_bound_chars


class UnsupportedMessage(ValueError):
    """The bridge must answer courier with a 4xx (non-retryable) and not call OAN."""


def map_courier_request(payload: dict[str, Any], *, purpose: str,
                        first_submitted_at: datetime) -> SmsDelivery:
    msg_uuid = str(payload.get("uuid", ""))
    try:
        uuid.UUID(msg_uuid)
    except ValueError:
        raise UnsupportedMessage("missing or invalid message uuid") from None
    if purpose == "otp":
        raise UnsupportedMessage("RapidPro must not send OTPs (eSignet owns OTP delivery)")
    to = str(payload.get("to", ""))
    if not _E164_ET.fullmatch(to):
        raise UnsupportedMessage("recipient is not an Ethiopian 9/7-series mobile")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise UnsupportedMessage("empty text")
    if not fits_one_sms(text):
        raise UnsupportedMessage("text does not fit one SMS (160 GSM-7 / 70 if Amharic)")
    return SmsDelivery(idempotency_key=f"rapidpro:{msg_uuid}", recipient=to, text=text,
                       source_reference=f"rapidpro:{msg_uuid}", purpose=purpose,
                       expires_at=None, first_submitted_at=first_submitted_at)
