"""Broadcasts on SMS and Telegram (Channels Specification §6.8) through the real DA rules.

The beneficiary API, Bot Services and the DA Registry are fakes; the connector writes real
OAN Message rows (nothing is sent: the job's commits are replaced, so no after-commit
dispatch runs). Test numbers only (0900000xxx).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime, today

from da_broadcast_channels import dispatch
from da_broadcast_channels.clients import beneficiary, bot_services
from da_broadcast_channels.clients.http import UpstreamError
from da_services.services import constants as C
from da_services.tests.fixtures import live_geo, set_da_registry_id
from da_services.utils.permissions import get_scope_context

COMMS = "bcc-comms@da.local"  # national desk: no scope on the grant
COMMS_W = "bcc-comms-w@da.local"  # one woreda only
DA_USER = "bcc-da@da.local"
DA_OTHER = "bcc-da2@da.local"
DA_ID, DA_OTHER_ID = "DA-BCC-0001", "DA-BCC-0002"
USERS = (COMMS, COMMS_W, DA_USER, DA_OTHER)


def _user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.add_roles(*[r for r in roles if r not in frappe.get_roles(email)])
	return email


def _grant(user, role, **kw):
	doc = frappe.new_doc("DA RBAC Assignment")
	doc.update({"user": user, "role": role, "effective_from": today(), "active": 1, **kw})
	doc.insert(ignore_permissions=True, ignore_links=True)


def _post(user, da_id, kebele, woreda):
	frappe.get_doc(
		{
			"doctype": "DA Kebele Assignment",
			"user": user,
			"da_id": da_id,
			"kebele": kebele,
			"woreda": woreda,
			"state": "Effective",
			"mode": "Manual",
			"effective_date": today(),
		}
	).insert(ignore_permissions=True, ignore_links=True)


class FakeBeneficiary:
	"""Farmers by area, paged by an index cursor. ``rows[area_code]`` = list of rows."""

	rows: ClassVar[dict[str, list[dict]]] = {}
	calls: ClassVar[list[tuple]] = []

	def farmers_by_area(self, level, code, cursor, page_size):
		FakeBeneficiary.calls.append((level, code, cursor, page_size))
		items = FakeBeneficiary.rows.get(code, [])
		start = int(cursor or 0)
		chunk = items[start : start + page_size]
		nxt = str(start + page_size) if start + page_size < len(items) else None
		return beneficiary.Page([beneficiary.to_farmer(r) for r in chunk], nxt)


class FakeBot:
	created: ClassVar[list[tuple[str, dict]]] = []
	finished = False
	sent: ClassVar[set[str]] = set()
	not_on_telegram: ClassVar[list[str]] = []
	fail: UpstreamError | None = None

	def create(self, key, body):
		if FakeBot.fail:
			raise FakeBot.fail
		FakeBot.created.append((key, body))
		return bot_services.Created("BOT-1", 3, list(FakeBot.not_on_telegram))

	def progress(self, broadcast_id):
		counts = dict.fromkeys(bot_services.STATUSES, 0) | {"sent": len(FakeBot.sent)}
		return bot_services.Progress(broadcast_id, FakeBot.finished, counts)

	def telegram_recipients(self, broadcast_id):
		return set(FakeBot.sent)


class FakeRegistry:
	phones: ClassVar[dict[str, str]] = {}

	def get_da(self, da_id):
		phone = FakeRegistry.phones.get(da_id)
		return type("Ref", (), {"phone": phone})() if phone else None


def _farmer(n, phone=True):
	row = {"farmer_id": f"FR-BCC-{n:04d}", "language_spoken": "am", "kebele_code": "K"}
	if phone:
		row["phones"] = [{"number": f"09000009{n:02d}", "is_primary": True}]
	return row


class TestBroadcastChannels(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.geo = live_geo()
		for u in USERS:
			frappe.db.delete("DA RBAC Assignment", {"user": u})
			frappe.db.delete("DA Kebele Assignment", {"user": u})
			frappe.db.delete("DA Broadcast Dispatch", {"sent_by": u})
			frappe.db.delete("DA Broadcast", {"sent_by": u})
		_user(COMMS, [C.ROLE_COMMS])
		_grant(COMMS, C.ROLE_COMMS)
		_user(COMMS_W, [C.ROLE_COMMS])
		_grant(COMMS_W, C.ROLE_COMMS, region_scope=self.geo["region"], woreda_scope=self.geo["woreda"])
		for user, da_id in ((DA_USER, DA_ID), (DA_OTHER, DA_OTHER_ID)):
			_user(user, [C.ROLE_DA])
			set_da_registry_id(user, da_id)
			_grant(
				user, C.ROLE_DA, da_id=da_id, region_scope=self.geo["region"], woreda_scope=self.geo["woreda"]
			)
			_post(user, da_id, self.geo["kebele"], self.geo["woreda"])

		FakeBeneficiary.rows = {self.geo["woreda"]: [_farmer(i) for i in range(1, 6)] + [_farmer(6, False)]}
		FakeBeneficiary.calls = []
		FakeBot.created, FakeBot.finished, FakeBot.sent, FakeBot.not_on_telegram = [], False, set(), []
		FakeBot.fail = None
		FakeRegistry.phones = {DA_ID: "0900000951", DA_OTHER_ID: "0900000952"}
		settings = frappe.get_single("DA Broadcast Channel Settings")
		settings.update({"sms_fallback_wait_minutes": 15, "page_size": 2})
		settings.save(ignore_permissions=True)

		patches = [
			mock.patch.object(beneficiary, "factory", FakeBeneficiary),
			mock.patch.object(bot_services, "factory", FakeBot),
			mock.patch(
				"da_services.integrations.da_registry.client.get_client", lambda *a, **k: FakeRegistry()
			),
			mock.patch.object(dispatch, "_commit", lambda: None),
			mock.patch.object(dispatch, "_rollback", lambda: None),
			mock.patch.object(dispatch, "enqueue", lambda name: None),
			mock.patch("oan_messaging_connector.api.enabled", lambda: True),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	# -- helpers ---------------------------------------------------------------

	def send(self, user, **kw):
		body = {
			"recipients": "farmers",
			"scope": "woreda",
			"scope_value": self.geo["woreda"],
			"channels": ["sms"],
			"title": "Fertiliser distribution",
			"body": "Collect vouchers at the Woreda office on Monday.",
			"source": "Informational",
			"client_op_id": str(uuid.uuid4()),
		} | kw
		return dispatch.create(get_scope_context(user), body)

	def dispatch_of(self, out):
		return dispatch.find(out["broadcast_id"])

	def sms_rows(self, name):
		return frappe.get_all(
			"OAN Message",
			filters={"reference_doctype": dispatch.DOCTYPE, "reference_name": name},
			fields=["recipient", "purpose", "source_reference", "text"],
			order_by="source_reference asc",
		)

	# -- who may send ----------------------------------------------------------

	def test_only_comms_or_admin_broadcast_to_farmers(self):
		with self.assertRaises(frappe.PermissionError):
			self.send(DA_USER, scope="woreda")
		self.assertTrue(self.send(COMMS)["state"] == "Queued")
		self.assertTrue(self.send("Administrator")["state"] == "Queued")

	def test_farmers_are_region_zone_or_woreda_and_never_in_app(self):
		with self.assertRaises(frappe.ValidationError):
			self.send(COMMS, scope="kebele", scope_value=self.geo["kebele"])
		with self.assertRaises(frappe.ValidationError):
			self.send(COMMS, channels=["in_app", "sms"])
		for scope in ("region", "zone"):
			self.assertEqual(self.send(COMMS, scope=scope, scope_value=self.geo[scope])["scope"], scope)

	def test_scoped_comms_stays_inside_their_grant(self):
		self.send(COMMS_W)
		with self.assertRaises(frappe.PermissionError):
			self.send(COMMS_W, scope_value=self.geo["foreign_woreda"])

	def test_unknown_area_code_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.send(COMMS, scope_value="ET99999999")

	def test_da_keeps_own_kebele_rule(self):
		out = self.send(
			DA_USER, recipients="das", scope="kebele", scope_value=self.geo["kebele"], channels=["sms"]
		)
		self.assertEqual(out["recipients"], "das")
		with self.assertRaises(frappe.PermissionError):
			self.send(DA_USER, recipients="das", scope="kebele", scope_value=self.geo["foreign_kebele"])

	# -- SMS length --------------------------------------------------------------

	def test_sms_copy_may_take_up_to_three_parts(self):
		out = self.send(COMMS, body="ቅ" * 150)  # Amharic: 67 per part -> 3 parts
		self.assertEqual(out["sms"]["parts"], 3)
		with self.assertRaises(frappe.ValidationError):
			self.send(COMMS, body="ቅ" * 202)
		# Telegram alone has no SMS limit.
		self.assertNotIn("sms", self.send(COMMS, body="ቅ" * 400, channels=["telegram"]))

	# -- farmers on SMS ----------------------------------------------------------

	def test_sms_to_farmers_pages_the_area_and_skips_no_phone(self):
		out = self.send(COMMS, source="Weather")
		name = self.dispatch_of(out)
		dispatch.run(name)
		doc = frappe.get_doc(dispatch.DOCTYPE, name)
		self.assertEqual((doc.state, doc.sms_cursor), ("Done", "end"))
		self.assertEqual((doc.sms_targeted, doc.sms_queued, doc.sms_no_phone), (6, 5, 1))
		self.assertEqual(len(FakeBeneficiary.calls), 3)  # page size 2
		rows = self.sms_rows(name)
		self.assertEqual({r.purpose for r in rows}, {"alert"})  # SMS-06: Weather -> alert
		self.assertEqual(rows[0].source_reference, f"da_broadcast:{doc.broadcast}:FR-BCC-0001")
		self.assertEqual(frappe.db.get_value("DA Broadcast", doc.broadcast, "recipients"), 6)
		self.assertEqual(dispatch.status(name)["sms"]["by_status"], {"queued": 5})

	def test_a_multi_part_sms_copy_is_queued(self):
		name = self.dispatch_of(self.send(COMMS, body="ቅ" * 150))  # 3 parts
		dispatch.run(name)
		self.assertEqual(frappe.db.get_value(dispatch.DOCTYPE, name, ["sms_queued", "sms_not_queued"]), (5, 0))

	def test_informational_goes_as_broadcast_purpose(self):
		name = self.dispatch_of(self.send(COMMS, source="Informational"))
		dispatch.run(name)
		self.assertEqual({r.purpose for r in self.sms_rows(name)}, {"broadcast"})

	def test_a_restart_resumes_after_the_last_saved_page(self):
		name = self.dispatch_of(self.send(COMMS))
		doc = frappe.get_doc(dispatch.DOCTYPE, name)
		doc.db_set({"state": "SMS sending", "sms_cursor": "4", "sms_targeted": 4, "sms_queued": 4})
		dispatch.run(name)
		self.assertEqual(len(self.sms_rows(name)), 1)  # farmer 5; farmer 6 has no phone
		self.assertEqual(frappe.db.get_value(dispatch.DOCTYPE, name, "sms_queued"), 5)

	# -- Telegram first ----------------------------------------------------------

	def test_telegram_first_then_sms_skips_farmers_reached(self):
		name = self.dispatch_of(self.send(COMMS, channels=["sms", "telegram"], expires_at=None))
		dispatch.run(name)
		doc = frappe.get_doc(dispatch.DOCTYPE, name)
		self.assertEqual((doc.state, doc.telegram_broadcast_id), ("Telegram sending", "BOT-1"))
		key, body = FakeBot.created[0]
		self.assertEqual((body["recipients"], body["scope"]["code"]), ("farmers", self.geo["woreda"]))
		self.assertEqual(key, doc.client_op_id)
		self.assertEqual(self.sms_rows(name), [])  # waits for Telegram

		FakeBot.finished, FakeBot.sent = True, {"FR-BCC-0001", "FR-BCC-0003"}
		dispatch.run(name)
		doc.reload()
		self.assertEqual((doc.state, doc.sms_skipped_on_telegram, doc.sms_queued), ("Done", 2, 3))
		self.assertEqual(len(FakeBot.created), 1)  # never created twice

	def test_sms_starts_after_the_wait_even_if_telegram_is_still_sending(self):
		name = self.dispatch_of(self.send(COMMS, channels=["sms", "telegram"]))
		dispatch.run(name)
		frappe.db.set_value(
			dispatch.DOCTYPE, name, "telegram_started_at", add_to_date(now_datetime(), minutes=-16)
		)
		FakeBot.sent = {"FR-BCC-0002"}
		dispatch.run(name)
		self.assertEqual(frappe.db.get_value(dispatch.DOCTYPE, name, "sms_queued"), 4)

	def test_agents_not_on_telegram_get_the_sms_copy(self):
		FakeBot.not_on_telegram, FakeBot.finished = [DA_OTHER_ID], True
		out = self.send(COMMS, recipients="das", scope="woreda", channels=["in_app", "sms", "telegram"])
		# The DA backend's own bell broadcast (the site may hold other agents of the woreda).
		self.assertGreaterEqual(out["in_app"]["recipients"], 2)
		self.assertTrue(frappe.db.exists("DA Notification", {"user": DA_OTHER, "kind": "Broadcast"}))
		name = self.dispatch_of(out)
		dispatch.run(name)
		rows = self.sms_rows(name)
		self.assertEqual([r.recipient for r in rows], ["0900000952"])

	def test_agent_broadcast_skips_the_sender(self):
		name = self.dispatch_of(
			self.send(
				DA_USER, recipients="das", scope="kebele", scope_value=self.geo["kebele"], channels=["sms"]
			)
		)
		dispatch.run(name)
		self.assertEqual([r.recipient for r in self.sms_rows(name)], ["0900000952"])

	# -- idempotency and failures ------------------------------------------------

	def test_replay_returns_the_original_and_a_changed_body_conflicts(self):
		op = str(uuid.uuid4())
		first = self.send(COMMS, client_op_id=op)
		again = self.send(COMMS, client_op_id=op)
		self.assertTrue(again["duplicate"])
		self.assertEqual(frappe.db.count(dispatch.DOCTYPE, {"client_op_id": op}), 1)
		self.assertEqual(again["broadcast_id"], first["broadcast_id"])
		with self.assertRaises(dispatch.Conflict):
			self.send(COMMS, client_op_id=op, title="Something else")

	def test_permanent_upstream_refusal_fails_and_a_timeout_retries(self):
		name = self.dispatch_of(self.send(COMMS, channels=["telegram"]))
		FakeBot.fail = UpstreamError("bot_services", "timeout")
		dispatch.run(name)
		self.assertEqual(
			frappe.db.get_value(dispatch.DOCTYPE, name, ["state", "error"]), ("Queued", "timeout")
		)
		FakeBot.fail = UpstreamError("bot_services", "validation_failed", 422)
		dispatch.run(name)
		self.assertEqual(frappe.db.get_value(dispatch.DOCTYPE, name, "state"), "Failed")

	def test_expired_broadcast_stops_the_sms_pass(self):
		soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
		name = self.dispatch_of(self.send(COMMS, expires_at=soon))
		frappe.db.set_value(dispatch.DOCTYPE, name, "expires_at", "2020-01-01T00:00:00+00:00")
		dispatch.run(name)
		self.assertEqual(frappe.db.get_value(dispatch.DOCTYPE, name, "error"), "expired")
		self.assertEqual(self.sms_rows(name), [])
