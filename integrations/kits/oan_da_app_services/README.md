# DA Services kit: farmer SMS for planned and rescheduled visits

| | |
|---|---|
| **Repository** | `oan_da_app_services` |
| **Base** | `develop` at `ce156bf` |
| **Patch** | `0001-feat-visits-farmer-SMS-for-planned-and-rescheduled-v.patch` (5 files) |
| **Handoff guide** | 13 |

## What it changes

| File | Change |
|---|---|
| `da_services/services/visit_sms.py` (new) | Templates, `render()`, `visit_deadline()`, `notify_farmer()` through `queue_sms` |
| `da_services/services/visits.py` | `plan_visit` raises `planned` after the insert; `reschedule_visit` raises `rescheduled` for the new visit |
| `da_services/hooks.py` | `required_apps` adds `oan_messaging_connector` |
| `da_services/tests/test_visit_sms.py` (new) | 8 tests through the real plan and reschedule services |
| `.github/workflows/ci.yml` | Fetches `oan_messaging_connector` before installing the app. Until the connector is published under the organisation, set the repository variable `OAN_MESSAGING_CONNECTOR_REPO` to a URL the CI can clone |

Sent only when the Development Agent ticks **Notify farmer by SMS**
(`notify_farmer_sms`, already on DA Visit), and only to the farmer, whose phone comes from
**DA Farmer Reference** (the cached registry record). Development Agents are alerted on
Telegram, not by SMS. Each event belongs to one visit row (a reschedule creates a new
row), so `source_reference = da:visit:<visit>:<event>` is unique, and an idempotent
replan (same `client_op_id`) sends nothing. The message expires when the visit starts,
capped inside the 7-day horizon; a past or closed visit, or a farmer without a phone,
sends nothing.

## Configure

Caller `da` (sms, `transactional`, plus `otp` for the Auth kit), then the connector
settings on each DA site.

## Evidence (17 November 2026)

| Check | Result |
|---|---|
| DA pre-commit hooks on the changed files | Pass |
| Semgrep: Frappe rules, Python correctness, shared OAN rules | 0 findings |
| Kit tests | 8 passed; DA's own visit tests 15 passed |
| Full DA suite with the kit / without | 930 run, 0 failures / 922 run, 0 failures |
| End to end on a DA site (`trial/stack/a2c/e2e/e2e_da.py`): a real `plan_visit` with the box ticked, then `reschedule_visit`, through the connector's after-commit job, OAN over HTTPS and the fake 8028 | Both messages `delivered`; the farmer received "will visit you on 18 Nov 2026 at 09:00" and then "has moved to 19 Nov 2026 at 10:30" |
| Module CI simulated (`ci.yml` steps in a fresh bench, connector from `OAN_MESSAGING_CONNECTOR_REPO`) | Connector fetched and installed; no failure that the module does not already have without the kit |

## Decisions DA owns

Templates and translations; whether cancellation is a message; the cut-over date.
