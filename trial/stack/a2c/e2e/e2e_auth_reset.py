"""Auth reset-code SMS end to end on the trial bench (test number only)."""

import frappe

frappe.init(site="a2c.trial", sites_path=".")
frappe.connect()
from oan_auth_service.api.v1 import auth  # noqa: E402

USER = "reset-e2e@example.invalid"
frappe.cache.delete_value(auth._otp_issued_cache_key(USER))
auth._deliver_reset_by_sms(USER, "0900000731")
frappe.db.commit()
print("reset issued; hash stored:", bool(frappe.cache.get_value(auth._otp_cache_key(USER))))
