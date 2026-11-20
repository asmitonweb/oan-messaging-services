"""Telegram: send plain text to an already-known chat ID.

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION.

* The user must first open the bot and press Start (or send it any message). Telegram
  does not let a bot start a conversation. Until then OAN reports ``failed`` /
  ``chat_not_found``.
* Obtaining and storing the chat ID is the calling service's onboarding. It is NOT part
  of OAN or of this example (no onboarding or conversational flows).
* The bot is chosen by OAN from the caller's authorized configuration (bound when the
  message is accepted). The request cannot name a bot, token or URL.
* ``sent`` means the Bot API accepted the message. Telegram has no handset delivery
  receipts, so ``delivered`` never appears for Telegram.
* Text is sent as plain text (no Markdown/HTML parsing), up to 4096 UTF-16 units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from oan_client import OanClient, Submission, TelegramDelivery, delivery_key


@dataclass(frozen=True, repr=False)
class TelegramNotice:
    service: str  # originating service, e.g. "grievance"
    event_id: str
    chat_id: int  # already known from the service's own onboarding
    message_text: str
    purpose: str = "transactional"

    def __repr__(self) -> str:
        return f"TelegramNotice(service={self.service!r}, event_id={self.event_id!r})"


def to_delivery(notice: TelegramNotice, first_submitted_at: datetime) -> TelegramDelivery:
    return TelegramDelivery(
        idempotency_key=delivery_key(notice.service, notice.event_id, "user", "telegram"),
        chat_id=notice.chat_id,
        text=notice.message_text,
        source_reference=f"{notice.service}:{notice.event_id}",
        purpose=notice.purpose,
        expires_at=None,
        first_submitted_at=first_submitted_at,
    )


async def send_notice(client: OanClient, notice: TelegramNotice,
                      first_submitted_at: datetime) -> Submission:
    return await client.submit(to_delivery(notice, first_submitted_at))
