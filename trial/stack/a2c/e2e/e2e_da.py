"""DA kit end to end on the trial bench (da.trial; test numbers only): plan a visit with
Notify farmer by SMS, then reschedule it; the connector sends through OAN to the fake 8028."""

import json
import time

import frappe

frappe.init(site="da.trial", sites_path=".")
frappe.connect()
frappe.set_user("Administrator")
frappe.flags.in_import = True  # fixture users: skip Frappe's per-hour user-creation throttle

from da_services.services import constants as C  # noqa: E402
from da_services.services import visits  # noqa: E402
from da_services.tests.fixtures import ensure_kebele_assignment, live_geo, set_da_registry_id  # noqa: E402
from da_services.tests.test_visits import _grant, _user  # noqa: E402
from frappe.utils import add_days, today  # noqa: E402
from oan_messaging_connector import outbox  # noqa: E402

DA, DA_ID, FARMER, PHONE = "e2e-da@da.local", "DA-000001", "FR-E2E-OAN-1", "0900000761"
geo = live_geo()
_user(DA, [C.ROLE_DA])
set_da_registry_id(DA, DA_ID)
_grant(DA, C.ROLE_DA, da_id=DA_ID, region_scope=geo["region"], woreda_scope=geo["woreda"])
ensure_kebele_assignment(DA_ID, geo["kebele"])
if not frappe.db.exists("DA Farmer Reference", FARMER):
    ref = frappe.get_doc(
        {
            "doctype": "DA Farmer Reference",
            "farmer_id": FARMER,
            "farmer_name": "E2E",
            "phone": PHONE,
        }
    )
    ref.flags.ignore_mandatory = True
    ref.insert(ignore_permissions=True)
frappe.db.commit()

planned = visits.plan_visit(
    farmer_id=FARMER,
    scheduled_at=f"{add_days(today(), 1)} 09:00:00",
    user=DA,
    notify_farmer_sms=True,
)
frappe.db.commit()
moved = visits.reschedule_visit(planned["name"], scheduled_at=f"{add_days(today(), 2)} 10:30:00")
frappe.db.commit()

names = [planned["name"], moved["name"]]
deadline = time.monotonic() + 90
while time.monotonic() < deadline:
    frappe.db.rollback()
    rows = frappe.get_all(
        "OAN Message",
        filters={"reference_name": ["in", names]},
        fields=["reference_name", "event", "desk_status", "oan_status"],
    )
    if len(rows) == 2 and all(r.desk_status == "Submitted" for r in rows):
        outbox.poll_statuses()
        frappe.db.commit()
        if all(
            r.oan_status == "delivered"
            for r in frappe.get_all(
                "OAN Message", filters={"reference_name": ["in", names]}, fields=["oan_status"]
            )
        ):
            break
    time.sleep(3)
print(
    json.dumps(
        frappe.get_all(
            "OAN Message",
            filters={"reference_name": ["in", names]},
            fields=["reference_name", "event", "desk_status", "oan_status"],
        ),
        indent=1,
    )
)
