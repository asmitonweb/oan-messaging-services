"""Who may send what, where, on which channel: checked before anything is written.

Development Agents as recipients keep the DA backend's own rule
(``services.notifications._assert_broadcast_scope_allowed``). Farmers as recipients are
for the Communications Officer and the OAN Administrator only, and only for a whole
region, zone or woreda (the FSD: "broadcast to the Communications Officer").
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import frappe
from frappe import _

from da_services.services import constants as C
from da_services.services import notifications as svc
from da_services.utils.permissions import ScopeContext
from oan_messaging_connector.sms import MAX_PARTS, sms_parts

RECIPIENTS = ("farmers", "das")
CHANNELS = ("in_app", "sms", "telegram")
FARMER_SCOPES = ("region", "zone", "woreda")
DA_SCOPES = ("region", "zone", "woreda", "kebele")
#: SMS-06: Emergency and Weather go as alert; Knowledge and Informational as broadcast.
SMS_PURPOSE = {
	"Emergency": "alert",
	"Weather": "alert",
	"Knowledge": "broadcast",
	"Informational": "broadcast",
}
#: The Master Data DocType that holds each level's codes.
AREA_DOCTYPE = {"region": "DA Region", "zone": "DA Zone", "woreda": "DA Woreda", "kebele": "DA Kebele"}
MAX_EXPIRY = timedelta(days=7)


@dataclass(frozen=True)
class Request:
	recipients: str
	scope: str
	scope_value: str
	channels: tuple[str, ...]
	title: str
	body: str | None
	source: str
	expires_at: str | None  # RFC 3339 with an offset, as sent
	client_op_id: str | None

	@property
	def sms_text(self) -> str:
		"""The SMS copy: the message as written (the screen's SMS preview), else the title."""
		return (self.body or "").strip() or self.title

	@property
	def sms_purpose(self) -> str:
		return SMS_PURPOSE[self.source]

	def digest(self) -> str:
		"""Fingerprint of everything that defines the broadcast (not the replay key)."""
		fields = {k: v for k, v in asdict(self).items() if k != "client_op_id"}
		return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _fail(message: str, exc=frappe.ValidationError):
	frappe.throw(message, exc)


def parse(data: dict) -> Request:
	"""Shape checks only (no permissions): the same message for every caller."""
	unknown = set(data) - {
		"recipients",
		"scope",
		"scope_value",
		"channels",
		"title",
		"body",
		"source",
		"expires_at",
		"client_op_id",
	}
	if unknown:
		_fail(_("Unknown field(s) in the request."))
	recipients = str(data.get("recipients") or "").strip().lower()
	if recipients not in RECIPIENTS:
		_fail(_("recipients must be one of: {0}").format(", ".join(RECIPIENTS)))
	scope = str(data.get("scope") or "").strip().lower()
	scope_value = str(data.get("scope_value") or "").strip()
	allowed = FARMER_SCOPES if recipients == "farmers" else DA_SCOPES
	if scope not in allowed:
		_fail(_("scope must be one of: {0}").format(", ".join(allowed)))
	if not scope_value:
		_fail(_("scope_value is required."))
	if not frappe.db.exists(AREA_DOCTYPE[scope], scope_value):
		_fail(_("{0} {1} is not in Master Data.").format(scope.capitalize(), scope_value))

	channels = data.get("channels")
	if not isinstance(channels, list) or not channels:
		_fail(_("channels must be a non-empty list of: {0}").format(", ".join(CHANNELS)))
	channels = tuple(sorted({str(c).strip().lower() for c in channels}))
	if any(c not in CHANNELS for c in channels):
		_fail(_("channels must be a non-empty list of: {0}").format(", ".join(CHANNELS)))
	if recipients == "farmers" and "in_app" in channels:
		_fail(_("In-app delivery reaches Development Agents only; choose SMS or Telegram for farmers."))

	title = str(data.get("title") or "").strip()
	if not title or len(title) > 140:
		_fail(_("title is required and must be 140 characters or fewer."))
	body = str(data.get("body") or "").strip() or None
	source = str(data.get("source") or "Informational").strip()
	if source not in SMS_PURPOSE:
		_fail(_("source must be one of: {0}").format(", ".join(SMS_PURPOSE)))

	expires_at = data.get("expires_at") or None
	if expires_at:
		try:
			when = datetime.fromisoformat(str(expires_at))
		except ValueError:
			when = None
		if when is None or when.tzinfo is None:
			_fail(_("expires_at must be RFC 3339 with a time zone offset."))
		now = datetime.now(when.tzinfo)
		if when <= now or when - now > MAX_EXPIRY:
			_fail(_("expires_at must be in the future and at most 7 days ahead."))
		expires_at = when.isoformat()

	req = Request(
		recipients=recipients,
		scope=scope,
		scope_value=scope_value,
		channels=channels,
		title=title,
		body=body,
		source=source,
		expires_at=expires_at,
		client_op_id=str(data.get("client_op_id") or "").strip() or None,
	)
	if "sms" in channels:
		parts = sms_parts(req.sms_text)
		if parts > MAX_PARTS:
			_fail(
				_(
					"The SMS copy would take {0} SMS parts; at most {1} are allowed. Shorten the message."
				).format(parts, MAX_PARTS)
			)
	return req


def authorize(ctx: ScopeContext, req: Request) -> None:
	"""Deny, never widen: the sender's grants must cover the area."""
	if req.recipients == "farmers":
		if not (ctx.unrestricted or ctx.has_any_role(C.ROLE_COMMS)):
			_fail(
				_("Only a Communications Officer or an OAN Administrator can broadcast to farmers."),
				frappe.PermissionError,
			)
	# The DA backend's rule: Admin anything; Communications Officer within their grant
	# (none = national desk); Supervisor / Executive with the DA role their own woreda;
	# Development Agent their own kebele.
	svc._assert_broadcast_scope_allowed(ctx, req.scope, req.scope_value)


def sms_expiry(req: Request) -> datetime | None:
	return datetime.fromisoformat(req.expires_at) if req.expires_at else None


def expired(req_or_iso: str | None) -> bool:
	if not req_or_iso:
		return False
	when = datetime.fromisoformat(req_or_iso)
	return when <= datetime.now(when.tzinfo)
