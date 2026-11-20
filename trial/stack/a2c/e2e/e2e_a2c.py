"""A2C end-to-end trial step (runs inside the bench, as Administrator, on a2c.trial).

Drives REAL A2C code paths: update_loan_status (the bank API function) for three
transitions, and A2C Visit Schedule insert/cancel (doc_events). Commits like a request
would; dispatch then happens in the RQ worker (enqueue_after_commit). Test numbers only.
Prints the OAN Message rows it created.
"""

import json

import frappe
from frappe.utils import add_days, nowdate

frappe.init(site="a2c.trial", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")

BANK = "E2E_OAN_BANK"
APP_PHONE, LEAD_PHONE = "0900000801", "0900000802"

bank_name = frappe.db.get_value("A2C Participating Bank", {"bank_code": BANK}, "name")
if not bank_name:
    bank_name = (
        frappe.get_doc(
            {
                "doctype": "A2C Participating Bank",
                "registered_city": "Addis Ababa",
                "kyc_document": "/private/files/e2e_kyc.pdf",
                "gro_name": "E2E GRO",
                "ops_name": "E2E Ops",
                "bank_name": "Trial Bank",
                "bank_code": BANK,
                "status": "In Review",
                "entity_type": "Commercial Bank",
                "registered_email": "e2e@example.invalid",
                "registered_phone": "+251900000899",
                "registered_region": "Addis Ababa",
                "registered_country": "Ethiopia",
            }
        )
        .insert(ignore_permissions=True)
        .name
    )
    frappe.db.commit()

app = frappe.get_doc(
    {
        "doctype": "A2C Loan Application",
        "first_name": "E2E",
        "last_name": "Farmer",
        "phone_number": APP_PHONE,
        "language": "English",
        "loan_amount": 5000,
        "requested_amount": 5000,
        "bank": bank_name,
        "loan_type": "Input Loan",
        "status": "Active",
        "region": "Addis Ababa",
        "woreda": "Gulele",
    }
)
app.flags.ignore_mandatory = True
app.flags.ignore_links = True
app.insert(ignore_permissions=True)
# Start as already submitted with consent (submission itself needs a full consent
# fixture; A2C rightly refuses it otherwise). The bank's stage moves below are real.
first = frappe.get_all(
    "A2C Loan Status Stage",
    filters={"bank": bank_name, "label": "Submitted"},
    fields=["stage_id", "label"],
    limit=1,
)[0]
for field, value in (
    ("workflow_state", "In Transition"),
    ("status", "In Transition"),
    ("stage_id", first.stage_id),
    ("stage_label", first.label),
):
    app.db_set(field, value)
frappe.db.commit()

from oan_a2c.api.v1.loan_applications import update_loan_status  # noqa: E402

for stage in ("Verified", "Approved", "Disbursed"):
    res = update_loan_status(application_id=app.name, status=stage)
    frappe.db.commit()
    print(f"update_loan_status {stage}: {res.get('status')} {res.get('message')}")

existing = frappe.db.get_value("A2C Lead", {"phone_number": LEAD_PHONE}, "name")
if existing:  # A2C blocks duplicate active leads per phone: reuse it
    lead = frappe.get_doc("A2C Lead", existing)
else:
    lead = frappe.get_doc(
        {
            "doctype": "A2C Lead",
            "phone_number": LEAD_PHONE,
            "first_name": "E2E",
            "last_name": "Lead",
            "lead_source": "Agent Entry",
        }
    )
    lead.flags.ignore_mandatory = True
    lead.flags.ignore_links = True
    lead.insert(ignore_permissions=True)
visit = frappe.get_doc(
    {
        "doctype": "A2C Visit Schedule",
        "lead": lead.name,
        "visit_date": add_days(nowdate(), 2),
        "visit_time": "10:00:00",
        "meeting_location": "Kebele 04 office",
        "status": "Scheduled",
    }
)
visit.flags.ignore_mandatory = True
visit.insert(ignore_permissions=True)
frappe.db.commit()
visit.reload()
visit.status = "Cancelled"
visit.save(ignore_permissions=True)
frappe.db.commit()

rows = frappe.get_all(
    "OAN Message",
    filters={"reference_name": ["in", [app.name, visit.name]]},
    fields=["name", "reference_name", "event", "desk_status", "recipient"],
    order_by="creation asc",
)
print(
    json.dumps(
        {"application": app.name, "visit": visit.name, "messages": [dict(r) for r in rows]},
        indent=1,
        default=str,
    )
)
