"""SMS and Telegram delivery of a DA broadcast (Channels Specification §6.8).

create()  records the broadcast and its DA Broadcast Dispatch in the request and queues
          run() after commit. In-app to Development Agents is the DA backend's own
          services.notifications.broadcast(), called unchanged.
run()     resumable background job, re-entered by the one-minute scheduler until Done:
          1. Telegram first: POST /broadcasts to Bot Services (once; Idempotency-Key).
          2. Waits until Telegram has finished or ``sms_fallback_wait_minutes`` passed.
          3. SMS copy through oan_messaging_connector.queue_sms, one page of recipients
             per transaction (the page's OAN Message rows and the saved cursor commit
             together, so a restart never queues a page twice): farmers from the
             beneficiary API minus the Farmer IDs reached on Telegram; agents not on
             Telegram (or every agent when Telegram is not ticked).
status()  what GET /broadcasts/<id> returns: per-channel counts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from da_broadcast_channels import rules
from da_broadcast_channels.clients import beneficiary, bot_services
from da_broadcast_channels.clients.http import UpstreamError
from da_services.services import constants as C
from da_services.services import notifications as notif
from da_services.utils import audit as audit_log
from da_services.utils.permissions import ScopeContext
from oan_messaging_connector.api import queue_sms
from oan_messaging_connector.sms import MAX_PARTS

DOCTYPE = "DA Broadcast Dispatch"
SETTINGS = "DA Broadcast Channel Settings"
OPEN_STATES = ("Queued", "Telegram sending", "SMS sending")
EVENT_QUEUED = "notification.broadcast.channels_queued"
EVENT_DONE = "notification.broadcast.channels_done"
_REF_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")


class Conflict(frappe.ValidationError):
	http_status_code = 409


# ---------------------------------------------------------------------------
# create (in the request)
# ---------------------------------------------------------------------------


def create(ctx: ScopeContext, data: dict) -> dict:
	req = rules.parse(data)
	rules.authorize(ctx, req)
	if req.client_op_id:
		replay = _replay(req)
		if replay:
			return replay

	if req.recipients == "das" and "in_app" in req.channels:
		# The DA backend's own broadcast: DA Broadcast record, bell notifications, audit.
		out = notif.broadcast(
			ctx,
			title=req.title,
			body=req.body,
			href="/notifications",
			scope=req.scope,
			scope_value=req.scope_value,
			client_op_id=req.client_op_id,
			source=req.source,
		)
		if out["duplicate"]:  # raced a replay that inserted first
			return _replay(req) or _fail_conflict()
		record = _broadcast_name(req.client_op_id, out["broadcast_id"])
		if not ({"sms", "telegram"} & set(req.channels)):
			return {"broadcast_id": out["broadcast_id"], "recipients": out["recipients"], "duplicate": False}
	else:
		record = _insert_broadcast_record(ctx, req)

	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"broadcast": record,
			"recipients": req.recipients,
			"scope": req.scope,
			"scope_value": req.scope_value,
			"state": "Queued",
			"send_in_app": int("in_app" in req.channels),
			"send_sms": int("sms" in req.channels),
			"send_telegram": int("telegram" in req.channels),
			"source": req.source,
			"sms_purpose": req.sms_purpose if "sms" in req.channels else None,
			"sms_parts": _parts(req) if "sms" in req.channels else 0,
			"expires_at": req.expires_at,
			"sent_by": ctx.user,
			"sender_role": _sender_role(ctx),
			"client_op_id": req.client_op_id,
			"payload_digest": req.digest(),
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	audit_log.record(
		EVENT_QUEUED,
		entity_type="DA Broadcast",
		entity_name=record,
		details={
			"recipients": req.recipients,
			"scope": req.scope,
			"scope_value": req.scope_value,
			"channels": list(req.channels),
		},
		user=ctx.user,
	)
	enqueue(doc.name)
	return status(doc.name) | {"duplicate": False}


def _parts(req: rules.Request) -> int:
	from oan_messaging_connector.sms import sms_parts

	return sms_parts(req.sms_text)


def _sender_role(ctx: ScopeContext) -> str:
	if ctx.unrestricted:
		return C.ROLE_ADMIN
	for role in (C.ROLE_COMMS, C.ROLE_SUPERVISOR, C.ROLE_EXECUTIVE, C.ROLE_DA):
		if ctx.has_any_role(role):
			return role
	return ""


def _broadcast_name(client_op_id: str | None, broadcast_id: str) -> str:
	if client_op_id:
		return frappe.db.get_value("DA Broadcast", {"client_op_id": client_op_id}, "name")
	return broadcast_id


def _insert_broadcast_record(ctx: ScopeContext, req: rules.Request) -> str:
	"""The DA Broadcast record (history and the unique client_op_id) without bell rows."""
	if req.recipients == "das" and not notif._broadcast_recipients(req.scope, req.scope_value):
		frappe.throw(
			_("No active Development Agent is assigned to {0} {1}; check the scope value.").format(
				req.scope, req.scope_value
			),
			frappe.ValidationError,
		)
	record = frappe.get_doc(
		{
			"doctype": "DA Broadcast",
			"title": req.title,
			"body": req.body,
			"source": req.source,
			"scope": req.scope,
			"scope_value": req.scope_value,
			"sent_by": ctx.user,
			"sent_at": now_datetime(),
			"client_op_id": req.client_op_id,
		}
	)
	record.flags.ignore_permissions = True
	frappe.db.savepoint("da_broadcast_channels")
	try:
		record.insert()
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback(save_point="da_broadcast_channels")
		_fail_conflict()
	return record.name


def _replay(req: rules.Request) -> dict | None:
	"""The original answer for a repeated client_op_id; a different body is a conflict."""
	record = frappe.db.get_value("DA Broadcast", {"client_op_id": req.client_op_id}, "name")
	if not record:
		return None
	row = frappe.db.get_value(DOCTYPE, {"broadcast": record}, ["name", "payload_digest"], as_dict=True)
	if row and row.payload_digest == req.digest():
		return status(row.name) | {"duplicate": True}
	if not row and req.recipients == "das" and req.channels == ("in_app",):
		# An in-app-only replay of the DA backend's own broadcast: its own answer.
		count = frappe.db.get_value("DA Broadcast", record, "recipients")
		return {"broadcast_id": req.client_op_id, "recipients": int(count or 0), "duplicate": True}
	_fail_conflict()


def _fail_conflict():
	frappe.throw(_("client_op_id was already used for a different broadcast."), Conflict)


# ---------------------------------------------------------------------------
# run (background job)
# ---------------------------------------------------------------------------


def enqueue(name: str) -> None:
	frappe.enqueue(
		"da_broadcast_channels.dispatch.run",
		name=name,
		queue="long",
		job_id=f"da_broadcast_dispatch:{name}",
		deduplicate=True,
		enqueue_after_commit=True,
	)


def resume_open() -> None:
	"""Scheduler (every minute): re-enter every unfinished dispatch."""
	for name in frappe.get_all(DOCTYPE, filters={"state": ("in", OPEN_STATES)}, pluck="name"):
		enqueue(name)


def _commit() -> None:
	"""The job commits per step and per page (tests replace this to keep their transaction)."""
	frappe.db.commit()


def _rollback() -> None:
	frappe.db.rollback()


def _settings():
	return frappe.get_cached_doc(SETTINGS)


def run(name: str) -> None:
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.state not in OPEN_STATES:
		return
	try:
		if doc.send_telegram and not _telegram_step(doc):
			return  # waiting for Telegram; the scheduler comes back
		if doc.send_sms:
			_sms_step(doc)
		_finish(doc, "Done")
	except UpstreamError as exc:
		_rollback()
		permanent = exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429)
		frappe.db.set_value(DOCTYPE, name, "error", exc.code[:140])
		frappe.log_error(f"Broadcast dispatch {name}: {exc}", "DA Broadcast Channels")
		if permanent:
			_finish(frappe.get_doc(DOCTYPE, name), "Failed")
		_commit()


def _telegram_step(doc) -> bool:
	"""True when the SMS copy may start (or Telegram is all there is and it is finished)."""
	bot = bot_services.get_client()
	if not doc.telegram_broadcast_id:
		created = bot.create(_idempotency_key(doc), _telegram_body(doc))
		doc.db_set(
			{
				"state": "Telegram sending",
				"telegram_broadcast_id": created.broadcast_id,
				"telegram_recipients": created.recipients,
				"not_on_telegram": json.dumps(created.not_on_telegram),
				"telegram_started_at": now_datetime(),
				"error": None,
			}
		)
		_commit()
	progress = bot.progress(doc.telegram_broadcast_id)
	doc.db_set({"telegram_counts": json.dumps(progress.counts), "telegram_finished": int(progress.finished)})
	_commit()
	if progress.finished:
		return True
	if not doc.send_sms:
		return False
	wait = int(_settings().sms_fallback_wait_minutes or 15)
	return now_datetime() >= add_to_date(get_datetime(doc.telegram_started_at), minutes=wait)


def _idempotency_key(doc) -> str:
	return doc.client_op_id or f"da-broadcast-{doc.broadcast}"


def _telegram_body(doc) -> dict:
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["title", "body"], as_dict=True)
	body = {
		"recipients": doc.recipients,
		"scope": {"level": doc.scope, "code": doc.scope_value},
		"sender_ref": frappe.db.get_value("User", doc.sent_by, "da_registry_id") or doc.sent_by,
		"sender_role": doc.sender_role,
		"source": doc.source,
		"source_reference": _ref(f"da_broadcast:{doc.broadcast}"),
		"title": record.title,
		"body": record.body or "",
	}
	if doc.expires_at:
		body["expires_at"] = doc.expires_at
	return body


def _sms_step(doc) -> None:
	if doc.state != "SMS sending":
		doc.db_set({"state": "SMS sending", "sms_started_at": now_datetime()})
		_commit()
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["title", "body"], as_dict=True)
	text = (record.body or "").strip() or record.title
	expires = datetime.fromisoformat(doc.expires_at) if doc.expires_at else None
	if doc.recipients == "farmers":
		_sms_farmers(doc, text, expires)
	else:
		_sms_agents(doc, text, expires)


def _sms_farmers(doc, text: str, expires: datetime | None) -> None:
	skip = (
		bot_services.get_client().telegram_recipients(doc.telegram_broadcast_id)
		if doc.send_telegram
		else set()
	)
	page_size = int(_settings().page_size or 500)
	client = beneficiary.get_client()
	cursor = doc.sms_cursor or None
	if cursor == "end":
		return
	for page in beneficiary.iter_pages(client, doc.scope, doc.scope_value, cursor, page_size):
		if rules.expired(doc.expires_at):
			doc.db_set("error", "expired")
			_commit()
			return
		counts = {"sms_targeted": 0, "sms_queued": 0, "sms_skipped_on_telegram": 0, "sms_no_phone": 0}
		counts["sms_not_queued"] = 0
		for farmer in page.farmers:
			counts["sms_targeted"] += 1
			if farmer.farmer_id in skip:
				counts["sms_skipped_on_telegram"] += 1
			elif not farmer.phone:
				counts["sms_no_phone"] += 1
			else:
				_queue_one(doc, farmer.phone, text, farmer.farmer_id, farmer.language, expires, counts)
		_save_page(doc, counts, page.next_cursor or "end")


def _sms_agents(doc, text: str, expires: datetime | None) -> None:
	"""Agents of the area minus the sender; with Telegram ticked, only those not on it."""
	if doc.sms_cursor == "end":
		return
	from da_services.integrations.da_registry.client import get_client as registry

	only = set(json.loads(doc.not_on_telegram or "[]")) if doc.send_telegram else None
	da_ids = _agent_ids(doc)
	counts = dict.fromkeys(
		("sms_targeted", "sms_queued", "sms_skipped_on_telegram", "sms_no_phone", "sms_not_queued"), 0
	)
	client = registry("service")
	for da_id in da_ids:
		counts["sms_targeted"] += 1
		if only is not None and da_id not in only:
			counts["sms_skipped_on_telegram"] += 1
			continue
		ref = client.get_da(da_id)
		phone = getattr(ref, "phone", None) if ref else None
		if not phone:
			counts["sms_no_phone"] += 1
			continue
		_queue_one(doc, phone, text, f"da:{da_id}", None, expires, counts)
	_save_page(doc, counts, "end")


def _agent_ids(doc) -> list[str]:
	users = [u for u in notif._broadcast_recipients(doc.scope, doc.scope_value) if u != doc.sent_by]
	if not users:
		return []
	rows = frappe.get_all(
		"DA RBAC Assignment",
		filters={"user": ("in", users), "role": C.ROLE_DA, "active": 1},
		fields=["user", "da_id"],
	)
	ids = {r.da_id for r in rows if r.da_id}
	ids |= {v for v in frappe.get_all("User", filters={"name": ("in", users)}, pluck="da_registry_id") if v}
	return sorted(ids)


def _queue_one(doc, phone, text, who, language, expires, counts) -> None:
	name = queue_sms(
		recipient=phone,
		text=text,
		purpose=doc.sms_purpose,
		source_reference=_ref(f"da_broadcast:{doc.broadcast}:{who}"),
		reference_doctype=DOCTYPE,
		reference_name=doc.name,
		event="broadcast_sms",
		language=language,
		expires_at=expires,
		max_parts=MAX_PARTS,  # broadcasts may be multi-part (SMS-09); module notices stay one
	)
	counts["sms_queued" if name else "sms_not_queued"] += 1


def _save_page(doc, counts: dict, cursor: str) -> None:
	"""The page's OAN Message rows and the cursor commit together."""
	values = {k: int(doc.get(k) or 0) + v for k, v in counts.items()}
	values["sms_cursor"] = cursor
	doc.db_set(values)
	_commit()


def _finish(doc, state: str) -> None:
	doc.db_set({"state": state, "finished_at": now_datetime()})
	if doc.recipients == "farmers":
		reached = max(int(doc.sms_targeted or 0), int(doc.telegram_recipients or 0))
		frappe.db.set_value("DA Broadcast", doc.broadcast, "recipients", reached, update_modified=False)
	audit_log.record(
		EVENT_DONE,
		entity_type="DA Broadcast",
		entity_name=doc.broadcast,
		details={"state": state, **{k: v for k, v in status(doc.name).items() if k in ("sms", "telegram")}},
		user=doc.sent_by,
	)
	_commit()


def _ref(value: str) -> str:
	return _REF_UNSAFE.sub("_", value)[:128]


# ---------------------------------------------------------------------------
# status (GET /broadcasts/<id>)
# ---------------------------------------------------------------------------


def status(name: str) -> dict:
	doc = frappe.get_doc(DOCTYPE, name)
	record = frappe.db.get_value("DA Broadcast", doc.broadcast, ["client_op_id", "recipients"], as_dict=True)
	out = {
		"broadcast_id": record.client_op_id or doc.broadcast,
		"recipients": doc.recipients,
		"scope": doc.scope,
		"scope_value": doc.scope_value,
		"state": doc.state,
		"channels": [c for c in ("in_app", "sms", "telegram") if doc.get(f"send_{c}")],
		"error": doc.error,
	}
	if doc.send_in_app:
		out["in_app"] = {"recipients": int(record.recipients or 0)}
	if doc.send_telegram:
		out["telegram"] = {
			"recipients": int(doc.telegram_recipients or 0),
			"not_on_telegram": len(json.loads(doc.not_on_telegram or "[]")),
			"finished": bool(doc.telegram_finished),
			**json.loads(doc.telegram_counts or "{}"),
		}
	if doc.send_sms:
		out["sms"] = {
			"purpose": doc.sms_purpose,
			"parts": int(doc.sms_parts or 0),
			"read": int(doc.sms_targeted or 0),
			"queued": int(doc.sms_queued or 0),
			"skipped_on_telegram": int(doc.sms_skipped_on_telegram or 0),
			"no_phone": int(doc.sms_no_phone or 0),
			"not_queued": int(doc.sms_not_queued or 0),
			"by_status": _sms_by_status(doc.name),
		}
	return out


def _sms_by_status(name: str) -> dict[str, int]:
	"""OAN Message rows of this dispatch, by OAN status (blank while not yet submitted)."""
	rows = frappe.get_all(
		"OAN Message",
		filters={"reference_doctype": DOCTYPE, "reference_name": name},
		fields=["oan_status", {"COUNT": "*", "as": "n"}],
		group_by="oan_status",
	)
	return {(r.oan_status or "queued"): int(r.n) for r in rows}


def find(broadcast_id: str) -> str | None:
	"""The dispatch for a broadcast_id (the client_op_id, or the DA Broadcast name)."""
	record = frappe.db.get_value("DA Broadcast", {"client_op_id": broadcast_id}, "name") or (
		broadcast_id if frappe.db.exists("DA Broadcast", broadcast_id) else None
	)
	return frappe.db.get_value(DOCTYPE, {"broadcast": record}, "name") if record else None


def can_read(ctx: ScopeContext, name: str) -> bool:
	if ctx.unrestricted or ctx.has_any_role(C.ROLE_COMMS):
		return True
	return frappe.db.get_value(DOCTYPE, name, "sent_by") == ctx.user
