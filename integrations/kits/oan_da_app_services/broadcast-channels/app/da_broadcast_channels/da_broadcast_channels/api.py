"""Broadcast routes on the DA Services gateway prefix.

POST /api/v1/da-services/broadcasts              send to farmers or agents on in-app, SMS, Telegram
GET  /api/v1/da-services/broadcasts/<broadcast_id> per-channel counts

The existing POST /notifications/broadcast (in-app to agents) is untouched; a request
here with only ``in_app`` to ``das`` does exactly what that route does.
"""

from __future__ import annotations

import threading

import frappe
from frappe import _

from da_broadcast_channels import dispatch
from da_services.api.router import route
from da_services.services import constants as C
from da_services.utils.permissions import require_roles
from oan_auth_service.api.utils import handle_api_errors, success_response

_REGISTERED = False
_LOCK = threading.Lock()
#: Same senders as POST /notifications/broadcast.
SENDER_ROLES = (C.ROLE_COMMS, C.ROLE_ADMIN, C.ROLE_DA)


@route("/broadcasts", methods=("POST",), summary="Broadcast to farmers or agents on in-app, SMS, Telegram")
@frappe.whitelist(methods=["POST"])
@handle_api_errors
def create_broadcast():
	"""Request body (JSON):

	    {
	        "recipients": "farmers" | "das",
	        "scope": "region" | "zone" | "woreda" | "kebele",   // farmers: no kebele
	        "scope_value": "ET040801",                         // Master Data code
	        "channels": ["sms", "telegram"],                   // and/or "in_app" for das
	        "title": "Fertiliser distribution starts Monday",  // required, <= 140
	        "body": "Collect vouchers at the Woreda office.",  // the SMS copy (<= 3 parts)
	        "source": "Weather",                               // Knowledge | Emergency | Weather | Informational
	        "expires_at": "2026-11-21T17:00:00+03:00",         // optional, <= 7 days ahead
	        "client_op_id": "<uuid>"                           // idempotency key
	    }

	Returns 200 with the dispatch status; a replay of the same client_op_id and body
	returns the original with ``duplicate: true``; the same key with another body is 409.
	"""
	ctx = require_roles(*SENDER_ROLES)
	data = frappe.request.get_json(silent=True) if frappe.request else None
	if not isinstance(data, dict):
		frappe.throw(_("A JSON object body is required."), frappe.ValidationError)
	out = dispatch.create(ctx, data)
	return success_response(
		data=out, message="Broadcast already sent." if out["duplicate"] else "Broadcast queued."
	)


@route("/broadcasts/<broadcast_id>", methods=("GET",), summary="Per-channel counts of a broadcast")
@frappe.whitelist()
@handle_api_errors
def get_broadcast(broadcast_id: str):
	ctx = require_roles(*C.DA_SERVICES_ROLES)
	name = dispatch.find(broadcast_id)
	if not name or not dispatch.can_read(ctx, name):
		frappe.throw(_("Broadcast {0} not found.").format(broadcast_id), frappe.DoesNotExistError)
	return success_response(data=dispatch.status(name))


def ensure_routes_registered() -> None:
	"""before_request: DA Services registers its routes first, then this app adds its own."""
	global _REGISTERED
	from da_services.api.router import ensure_routes_registered as ensure_da_routes

	ensure_da_routes()
	if _REGISTERED:
		return
	with _LOCK:
		if _REGISTERED:
			return
		import frappe.api

		from oan_auth_service.api.router import _rules

		for rule in _rules:
			if rule not in frappe.api.API_URL_MAP._rules:
				frappe.api.API_URL_MAP.add(rule.empty())
		_REGISTERED = True
