# Copyright (c) 2026, OpenAgriNet and contributors
# For license information, please see license.txt

"""DA Broadcast Dispatch: the SMS and Telegram delivery of one DA Broadcast.

Written by da_broadcast_channels.dispatch; read by GET /broadcasts/<id>. Rows are history.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class DABroadcastDispatch(Document):
	def on_trash(self):
		frappe.throw(_("Broadcast deliveries are history; they cannot be deleted."))
