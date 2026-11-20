# Auth kit: password-reset codes through OAN Messaging

| | |
|---|---|
| **Repository** | `oan_auth_service` |
| **Base** | `develop` at `34c7306` |
| **Patch** | `0001-feat-sms-send-password-reset-codes-through-the-OAN-M.patch` (3 files) |
| **Handoff guide** | 14 |

## What it changes

| File | Change |
|---|---|
| `oan_auth_service/api/reset_sms.py` (new) | `oan_ready()`, `enqueue_reset_sms()`, and the job `send_reset_sms()` that sends with `oan_messaging_connector.send_now.send_sms_now` |
| `oan_auth_service/api/v1/auth.py` | `_deliver_reset_by_sms` uses OAN when the site has it configured; otherwise Frappe's own SMS sender, exactly as before |
| `oan_auth_service/tests/test_reset_sms.py` (new) | 7 tests (skipped where the connector is not installed) |

The code is sent once, after the request commits, by a job that holds it only in its
arguments: `purpose="otp"`, a UUID4 key fixed at issue, expiring with the code (at most
30 minutes, OAN's OTP ceiling), `source_reference = auth:password-reset:<reset_request_id>`.
Logs and the Error Log carry `reset_request_id`, the OAN id and error codes only; the job
catches every failure, so nothing it holds reaches Frappe's job-failure log. An
uncertain outcome is retried by the client with the same key inside the job and is not
repeated later: the user can ask for a new code within the daily cap. The requester's
response never changes.

Auth does **not** require the connector: on a site without it nothing changes. Auth's own
CI does not install the connector, so the 7 kit tests are skipped there; run them on a site
with the connector (as here), or add it to that CI.

## Configure

Auth uses the connector settings of the site it runs on. That site's caller must be
allowed `otp` as well as `transactional` (for example `a2c`, `grievance`, `da`).
Connector `develop` at `4ad8507` or later (`send_sms_now`).

## Evidence (17 November 2026)

| Check | Result |
|---|---|
| Auth pre-commit hooks (including gitleaks) on the changed files | Pass |
| Semgrep: Frappe rules, Python correctness, Auth `.semgrep/rules.yml` | 0 findings |
| Kit tests | 7 passed |
| Full Auth suite with the kit / without | 391 run, 0 failures / 384 run, 0 failures |
| End to end (`trial/stack/a2c/e2e/e2e_auth_reset.py`): a phone-only reset on the trial bench, through the worker, OAN over HTTPS and the fake 8028 | One `otp` message accepted and sent; one SMS at the gateway; no code in any log |
