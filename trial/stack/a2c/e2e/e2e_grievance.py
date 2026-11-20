"""Grievance kit end to end on the trial bench (grievance.trial; test numbers only).

A real Grievance and Notification, Grievance's own queue() and dispatch_queued(); the
row is handed to the connector, which sends after commit through OAN over HTTPS to the
fake 8028. Prints the row and its OAN Message.
"""

import json
import time

import frappe

frappe.init(site="grievance.trial", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

from oan_grievance_service.services import notifications  # noqa: E402
from oan_grievance_service.setup.install import seed_role_levels  # noqa: E402
from oan_grievance_service.tests.fixtures import a_leaf_area  # noqa: E402
from oan_messaging_connector import outbox  # noqa: E402

EVENT = "e2e_oan_sms_event"
HEAD, HEAD_PHONE = "e2e-head@example.invalid", "0900000751"

seed_role_levels()
if not frappe.db.exists("User", HEAD):
    frappe.get_doc(
        {
            "doctype": "User",
            "email": HEAD,
            "first_name": "E2E Head",
            "send_welcome_email": 0,
            "mobile_no": HEAD_PHONE,
        }
    ).insert(ignore_permissions=True)
frappe.get_doc("User", HEAD).add_roles("Grievance Officer")
dept = (
    frappe.db.get_value("Grievance Department", {"dept_name": "E2E OAN Dept"}, "name")
    or frappe.get_doc(
        {"doctype": "Grievance Department", "dept_name": "E2E OAN Dept", "short_name": "EOD"}
    )
    .insert(ignore_permissions=True)
    .name
)
if not frappe.db.exists("Grievance RBAC Assignment", {"department_scope": dept}):
    frappe.get_doc(
        {
            "doctype": "Grievance RBAC Assignment",
            "department_scope": dept,
            "active": 1,
            "effective_from": frappe.utils.today(),
            "officers": [
                {"user": HEAD, "role_level": "department_head", "is_primary": 1, "active": 1}
            ],
        }
    ).insert(ignore_permissions=True)
gtype = (
    frappe.db.get_value("Grievance Type", {"type_name": "E2E OAN Type"}, "name")
    or frappe.get_doc(
        {"doctype": "Grievance Type", "type_name": "E2E OAN Type", "service_category": "Inputs"}
    )
    .insert(ignore_permissions=True)
    .name
)
if not frappe.db.exists("Notification", f"Grievance: E2E OAN ({EVENT})"):
    frappe.get_doc(
        {
            "doctype": "Notification",
            "name": f"Grievance: E2E OAN ({EVENT})",
            "subject": '{{ _("Test") }}',
            "document_type": "Grievance",
            "event": "Method",
            "method": EVENT,
            "channel": "SMS",
            "grievance_recipient": notifications.RECIPIENT_DEPARTMENT_HEAD,
            "message": '{{ _("Grievance {0} test", context="grievance.test").format(doc.name) }}',
            "message_type": "Plain Text",
            "is_standard": 0,
            "enabled": 1,
        }
    ).insert(ignore_permissions=True)
grievance = frappe.get_doc(
    {
        "doctype": "Grievance",
        "submitter_type": "Individual Farmer",
        "submitter_name": "E2E Submitter",
        "submission_channel": "Mobile App",
        "administrative_area": a_leaf_area(),
        "service_category": "Inputs",
        "grievance_type": gtype,
        "description": "OAN Messaging end-to-end trial",
        "assigned_dept": dept,
        "contact_mobile": "0900000752",
    }
).insert(ignore_permissions=True)
frappe.db.commit()

rows = notifications.queue(grievance, EVENT)
frappe.db.commit()
notifications.dispatch_queued(
    grievance=grievance.name
)  # commits per row; the connector sends after commit

row = frappe.get_doc("Grievance Notification Log", rows[0].name)
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    frappe.db.rollback()
    message = frappe.get_doc("OAN Message", row.oan_message)
    if message.desk_status == "Submitted":
        outbox.poll_statuses()
        frappe.db.commit()
        message.reload()
        if message.oan_status == "delivered":
            break
    time.sleep(2)

print(
    json.dumps(
        {
            "row_status": row.status,
            "oan_message": row.oan_message,
            "desk_status": message.desk_status,
            "oan_status": message.oan_status,
            "source_reference": message.source_reference,
        },
        indent=1,
    )
)

# Leave no routing behind: this department's assignment would otherwise route other cases.
for name in frappe.get_all(
    "Grievance RBAC Assignment", filters={"department_scope": dept}, pluck="name"
):
    frappe.db.set_value("Grievance RBAC Assignment", name, "active", 0)
frappe.db.commit()
