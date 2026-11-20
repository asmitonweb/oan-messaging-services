"""Bot Services client: the Telegram copy of a broadcast (Channels Specification §6.8.2).

    POST {base}/broadcasts                       Idempotency-Key: <client_op_id>
         -> 202 new / 200 replay {"broadcast_id", "recipients", "not_on_telegram": [da_ref]}
    GET  {base}/broadcasts/{id}                  -> {"broadcast_id", "state", "counts": {...}}
    GET  {base}/broadcasts/{id}/telegram-recipients?cursor=
                                                 -> {"farmer_ids": [...], "next_cursor"}

``state`` is ``running`` or ``finished``. ``counts`` uses OAN Messaging's status names:
accepted, sending, sent, failed, expired, unknown. Until Bot Services is live, point
``bot_services.base_url`` at the kit's stub server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

from da_broadcast_channels.clients.http import call, upstream

STATUSES = ("accepted", "sending", "sent", "failed", "expired", "unknown")


@dataclass(frozen=True)
class Created:
	broadcast_id: str
	recipients: int
	not_on_telegram: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Progress:
	broadcast_id: str
	finished: bool
	counts: dict[str, int]


class BotServicesClient(Protocol):
	def create(self, idempotency_key: str, body: dict) -> Created: ...

	def progress(self, broadcast_id: str) -> Progress: ...

	def telegram_recipients(self, broadcast_id: str) -> set[str]: ...


class HttpBotServicesClient:
	def create(self, idempotency_key: str, body: dict) -> Created:
		_, res = call(
			upstream("bot_services"),
			"POST",
			"/broadcasts",
			json=body,
			headers={"Idempotency-Key": idempotency_key},
			ok=(200, 202),
		)
		return Created(
			broadcast_id=str(res["broadcast_id"]),
			recipients=int(res.get("recipients") or 0),
			not_on_telegram=[str(r) for r in res.get("not_on_telegram") or []],
		)

	def progress(self, broadcast_id: str) -> Progress:
		_, res = call(upstream("bot_services"), "GET", f"/broadcasts/{quote(broadcast_id, safe='')}")
		counts = res.get("counts") or {}
		return Progress(
			broadcast_id=broadcast_id,
			finished=res.get("state") == "finished",
			counts={s: int(counts.get(s) or 0) for s in STATUSES},
		)

	def telegram_recipients(self, broadcast_id: str) -> set[str]:
		"""Farmer IDs whose Telegram copy is ``sent``: the SMS copy skips exactly these."""
		up, ids, cursor = upstream("bot_services"), set(), None
		path = f"/broadcasts/{quote(broadcast_id, safe='')}/telegram-recipients"
		while True:
			_, res = call(up, "GET", path, params={"cursor": cursor} if cursor else None)
			ids.update(str(i) for i in res.get("farmer_ids") or [])
			cursor = res.get("next_cursor")
			if not cursor:
				return ids


#: Tests replace this with a fake; production uses the HTTP client.
factory = HttpBotServicesClient


def get_client() -> BotServicesClient:
	return factory()
