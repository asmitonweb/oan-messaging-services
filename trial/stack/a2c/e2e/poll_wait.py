"""Wait until every OAN Message has finished polling, then print the outbox (trial)."""

import time

import frappe

frappe.init(site="a2c.trial", sites_path=".")
frappe.connect()
end = time.monotonic() + 240
while time.monotonic() < end:
    frappe.db.rollback()
    rows = frappe.get_all(
        "OAN Message",
        order_by="creation asc",
        fields=[
            "name",
            "event",
            "desk_status",
            "oan_status",
            "oan_final",
            "poll_complete",
            "needs_attention",
            "polls",
        ],
    )
    if rows and all(r.poll_complete for r in rows):
        break
    time.sleep(5)
for r in rows:
    print(
        r.name,
        r.event,
        r.desk_status,
        r.oan_status,
        "final" if r.oan_final else "",
        "attention" if r.needs_attention else "",
        "polls",
        r.polls,
    )
