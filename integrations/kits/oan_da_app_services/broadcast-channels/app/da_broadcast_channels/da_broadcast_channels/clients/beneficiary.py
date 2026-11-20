"""Beneficiary API client: the registered farmers of an area, with their phones.

The route is the one the farmer registry owner is asked to provide (docs/BENEFICIARY-API-
REQUIREMENTS.md in the kit; spec §8.4 ID-01 and ID-04). Until it is live, point
``beneficiary_api.base_url`` at the kit's stub server.

    GET {base}/api/v1/farmers?level=woreda&code=ET040801&record_status=ACTIVE
                             &page_size=500&cursor=<opaque>
    200 {"items": [FarmerRow...], "next_cursor": "<opaque>" | null}

    GET {base}/api/v1/farmers/{farmer_id}          -> FarmerRow (404 when unknown)

``level`` is region, zone, woreda or kebele; ``code`` is the Master Data
``level_value_id`` the DA backend already keys its DA Region / Zone / Woreda / Kebele on.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from da_broadcast_channels.clients.http import UpstreamError, call, upstream

LEVELS = ("region", "zone", "woreda", "kebele")


@dataclass(frozen=True)
class Farmer:
	farmer_id: str
	phone: str | None  # the primary registered phone, None when the farmer has none
	language: str | None
	kebele_code: str | None


@dataclass(frozen=True)
class Page:
	farmers: list[Farmer]
	next_cursor: str | None


class BeneficiaryClient(Protocol):
	def farmers_by_area(self, level: str, code: str, cursor: str | None, page_size: int) -> Page: ...


def primary_phone(row: dict) -> str | None:
	"""``primary_phone`` when given, else the phone marked primary, else the first one."""
	if row.get("primary_phone"):
		return row["primary_phone"]
	phones = [p for p in row.get("phones") or [] if p.get("number")]
	primary = [p for p in phones if p.get("is_primary")]
	return (primary or phones or [{}])[0].get("number")


def to_farmer(row: dict) -> Farmer:
	if not row.get("farmer_id"):
		raise UpstreamError("beneficiary_api", "row_without_farmer_id")
	return Farmer(
		farmer_id=str(row["farmer_id"]),
		phone=primary_phone(row),
		language=row.get("language_spoken"),
		kebele_code=row.get("kebele_code"),
	)


class HttpBeneficiaryClient:
	def farmers_by_area(self, level: str, code: str, cursor: str | None, page_size: int) -> Page:
		if level not in LEVELS:
			raise ValueError(f"level must be one of {LEVELS}")
		params = {"level": level, "code": code, "record_status": "ACTIVE", "page_size": page_size}
		if cursor:
			params["cursor"] = cursor
		_, body = call(upstream("beneficiary_api"), "GET", "/api/v1/farmers", params=params)
		return Page(
			farmers=[to_farmer(r) for r in body.get("items") or []],
			next_cursor=body.get("next_cursor") or None,
		)

	def farmer(self, farmer_id: str) -> Farmer | None:
		try:
			_, body = call(upstream("beneficiary_api"), "GET", f"/api/v1/farmers/{quote(farmer_id, safe='')}")
		except UpstreamError as exc:
			if exc.status == 404:
				return None
			raise
		return to_farmer(body)


def iter_pages(
	client: BeneficiaryClient, level: str, code: str, cursor: str | None, page_size: int
) -> Iterator[Page]:
	"""Pages from ``cursor`` (None = the start) until the API reports no next cursor."""
	while True:
		page = client.farmers_by_area(level, code, cursor, page_size)
		yield page
		if not page.next_cursor:
			return
		cursor = page.next_cursor


#: Tests replace this with a fake; production uses the HTTP client.
factory = HttpBeneficiaryClient


def get_client() -> BeneficiaryClient:
	return factory()
