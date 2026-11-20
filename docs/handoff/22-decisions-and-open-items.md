# 22 Decisions and open items

## 1. Decisions each team owns

| Team | Decision |
|---|---|
| **Identity owner + DPI** | **The beneficiary API route for a farmer's registered phone (and Master Data kebele code)**: its access rules and what happens when there is no phone. Agreed direction: every farmer SMS goes to the registered phone. Used by the Consent Manager resolver, the Telegram linking OTP, and later by Credit, Grievance and DA. |
| DPI | Fix the Consent Manager build: the `openg2p-fastapi-common` pin (works with `9ddccc00f5`, or update to the new `crypto` package) and the missing `greenlet`; partner display names; Amharic OTP text |
| Credit | Which stage labels mean "approved" and "disbursed" for each bank (only a "Disbursed" stage sends the "paid out" SMS); bank short names; whether agents get copies; translations |
| Grievance | Shortened templates (one is 204 characters; seven exceed 70); cut-over date per site; whether to use the `send_sms` hook as an interim step ([12](12-grievance.md) §6) and when to remove it |
| DA | The plan and reschedule call sites; recipient roles; templates; whether cancellation is a message; the agent's phone source (DA backend roster) |
| Auth | Merging the kit (the explicit OTP job) or relying on a module site's `send_sms` hook in the interim ([14](14-auth.md) §6); pre-check recipient format before issuing; which site hosts the Telegram linking routes |
| eSignet | Confirm the assumed field names; remove the old gateway path |
| DevOps | Dev and staging URLs; a private CA that satisfies strict verification; secrets for tokens; scheduler and workers (including the `short` queue) on every Frappe site |
| **ATI** | **Named contacts** for each role in README §6 (OAN operator, DevOps, identity owner, project lead); where the connector and client repositories are published |

**Agreed for all modules:** explicit calls (`queue_sms`, or the OTP sender) are the
preferred integration. The connector's Frappe `send_sms` hook is supported only for code
that already calls Frappe's SMS sender (Grievance and Auth today), as an interim step,
and only when a module opts in from its own `hooks.py` ([03](03-frappe-connector.md) §7).

## 2. Known gaps and follow-ups

| Item | Owner | Notes |
|---|---|---|
| `queue_telegram()` in the connector | Connector | When a Frappe module needs Telegram |
| Consent notices (granted, denied, revoked, expired, shared) | DPI | Phase 2; templates exist |
| A2C `submit_application` end-to-end test | A2C / QA | Needs an approved-consent fixture |
| 8028: `sms_id` is assumed never to repeat (receipts match on it) | OAN / 8028 contract | Confirm with 8028 |
| OAN Messaging service | OAN | In independent review; not deployed. Release-blocker status: `EVIDENCE/RELEASE-BLOCKERS-STATUS.md` (12 of 16 fixed, pending review). Dev and staging will run the accepted version. |
| Connector and client published as repositories | ATI, OAN | Today they ship as files in the developer package (`code/`). The Credit, Grievance and DA kits fetch the connector in CI from the repository variable `OAN_MESSAGING_CONNECTOR_REPO` until then. The team is creating the shared repository; set the variable to its URL when it exists |
| A2C pre-existing test failures (7 at `f6bb735`: `test_farmer_b2c` and loan-product checks) | A2C | Unrelated to messaging; identical with and without the Credit kit |
| Controlled live acceptance with real 8028 and Telegram | QA, DevOps | After the dev environment |
| Telegram notices from modules to linked farmers | Product, Telegram bot team | Not in Phase 1. Route to decide when needed: through the bot (RapidPro API) or OAN's Telegram endpoint ([16](16-telegram.md)) |
| Telegram linking OTP routes in Auth | Auth | Issue and verify routes for the Telegram bot's farmer linking; same OTP sender pattern ([14](14-auth.md) §7) |
