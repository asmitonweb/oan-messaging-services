# Credit (A2C) kit: farmer SMS for the loan lifecycle

| | |
|---|---|
| **Repository** | `oan_a2c_service` |
| **Base** | `develop` at `f6bb735` |
| **Patch** | `0001-feat-sms-farmer-SMS-for-the-loan-lifecycle-through-o.patch` (8 files) |
| **Handoff guide** | 10 |

## What it changes

| File | Change |
|---|---|
| `oan_a2c/oan_messaging/events.py` (new) | Templates, `render()` (one SMS part at the longest field values), `event_for_transition()`, `visit_deadline()` |
| `oan_a2c/oan_messaging/notify.py` (new) | `notify_application()`, `notify_status_change()`, visit hooks, all through `queue_sms` |
| `oan_a2c/api/v1/farmer/applications.py` | `submit_application` raises `application_submitted` |
| `oan_a2c/api/v1/loan_applications.py` | `update_loan_status` raises the status or stage event when the status or stage changed |
| `oan_a2c/hooks.py` | `required_apps` adds `oan_messaging_connector`; A2C Visit Schedule `after_insert` and `on_update` |
| `oan_a2c/tests/test_farmer_sms.py` (new) | 15 tests |
| `.github/workflows/ci.yml` | Fetches `oan_messaging_connector` before installing the app. Until the connector is published under the organisation, set the repository variable `OAN_MESSAGING_CONNECTOR_REPO` to a URL the CI can clone |

Events: `application_submitted`, `stage_changed`, `application_approved`,
`application_rejected`, `loan_disbursed` (only for a "Disbursed" stage),
`application_completed`, `visit_scheduled`, `visit_cancelled`. Purpose `transactional`;
`source_reference = a2c:<record>:<event>`; visit messages expire when the visit starts.
No amounts, rejection reasons or staff free text. A stage move on an application that is
already closed (Completed or Rejected) sends nothing, except a late "paid out". The visit
hooks never fail the visit action: an SMS problem is logged and the save goes on.

## Configure

Caller `a2c` (sms, `transactional`; add `otp` if the Auth kit is applied), then the
connector settings on each A2C site (see `../README.md`).

## Evidence (17 November 2026)

| Check | Result |
|---|---|
| A2C pre-commit hooks on the changed files | Pass |
| Semgrep: Frappe rules, Python correctness, A2C `.semgrep/rules.yml` | 0 findings |
| Kit tests | 15 passed |
| Full A2C suite with the kit / without | 296 run, 7 fail / 281 run, 7 fail: the **same 7** pre-existing failures (`test_farmer_b2c` and loan-product checks), none from the kit |
| End to end: `update_loan_status` (Verified, Approved, Disbursed), visit scheduled and cancelled, through the worker, OAN over HTTPS and the fake 8028 | 5 messages, each `delivered` after one poll, each reaching the gateway exactly once |
| Module CI simulated (`ci.yml` steps in a fresh bench, connector from `OAN_MESSAGING_CONNECTOR_REPO`) | Connector fetched and installed; no failure that the module does not already have without the kit |

## Decisions A2C owns

Approved and disbursed stage labels per bank; bank short names (at most 30 characters);
translations; whether agents get copies; whether reschedules notify.
