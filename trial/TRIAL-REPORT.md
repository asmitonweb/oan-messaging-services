# OAN Messaging integration trial: Credit (A2C) and DPI Consent Manager

> **Historical record (15 November 2026).** The trial patches described here are now the
> integration kits in `integrations/kits/`, rebased on later module commits and
> re-tested on 17 November 2026; see each kit's README. Paths below are those of the
> original trial workspace.

15 November 2026. Prepared by the OAN Messaging project. Status: **Review**: trial evidence and patches for the
owning teams. The patches live on local branches; nothing was pushed, deployed or sent to
a real phone.

**Question answered:** does the integration work in a dev or staging-like environment?

**Method:** a local Docker network running the real OAN image, real Frappe v16 and
Consent Manager builds, and a fake 8028 gateway that speaks the real protocol.

## 1. What was built

| Deliverable | Where | Notes |
|---|---|---|
| **`oan_messaging_connector`**: a reusable Frappe app | `repos/oan_messaging_connector` (local git); `patches/oan_messaging_connector.{bundle,tar.gz}` | The OAN Message outbox, OAN Messaging Settings, after-commit dispatch, a 1-minute sweep, status polling, the one-SMS check and `queue_sms()`. Any Frappe caller can install it: A2C, Grievance, DA (Auth reuses only its settings and client for OTPs). |
| **Credit (`oan_a2c_service`)** patch | branch `oan-messaging-sms`; `patches/oan_a2c_service/` | Thin: adds `required_apps`, templates and event mapping, 2 loan call sites and 2 visit hooks. 7 files, +345 lines. |
| **Consent Manager** patch | branch `oan-messaging-otp`; `patches/consent-management/` (2 commits) | Delivers OTPs by SMS through OAN (off by default), masks OTP codes in logs, fails visibly (422/502/503) when a code cannot be delivered, vendors the client. 10 files, +1055 lines, most of it the vendored client. |
| **OAN fix** (found by this trial) | `oan_messaging_services-msg08/oan_messaging/redaction.py`, plus a regression test | Log redaction broke uvicorn access logs: see §4. |
| **Trial stack** | `stack/` | OAN with least-privilege roles and TLS, a fake 8028 gateway (mTLS plus receipt callbacks), a Frappe v16 bench and the Consent Manager. |

## 2. Environment (closer to staging than unit tests)

* **OAN:** the msg08 image (API over **HTTPS**, worker, receipts, cleanup), PostgreSQL 16
  with **separate owner and runtime roles** and `grants.sql` applied.
* **Fake 8028 gateway:** **mTLS** with a client certificate, the 8028-shaped response,
  then the **delivery-receipt callback** to the receipts ingress over pinned mTLS.
* **Credit:** Frappe **16.56.0** on Python **3.14**, MariaDB 11.8, Redis, `oan_auth_service`
  plus `oan_a2c` plus the connector, with a real RQ worker and scheduler.
* **Consent Manager:** its own image and PostgreSQL. Auth is off (dev mode), as its
  sanity suite allows.
* Test numbers only (`0900000xxx`); nothing can leave the Docker network.

## 3. Results

### OAN path (smoke)

API (HTTPS) → worker → fake 8028 (mTLS) → receipt callback (pinned mTLS) → `delivered`.
**Passed.**

### Credit (A2C)

| Check | Result |
|---|---|
| A2C test suite **before** the patch (develop 3887b8b) | 293 ran: 3 failures, 3 errors, **all pre-existing** (`test_farmer_b2c`) |
| A2C test suite **with** the patch | 299 ran (6 new, all pass). Failure set **identical** to the baseline: no regressions |
| Connector test suite | **11/11 pass** (fake OAN) |
| End to end on the real site | `update_loan_status` Verified → Approved → Disbursed, plus a visit scheduled then cancelled: messages **queued → dispatched by the RQ worker → OAN → gateway → receipt → Desk shows Submitted / delivered (final)**. 9/9 messages, each sent once, one poll each, none needing attention |
| Texts as received by the "handset" | e.g. "OAN Credit: Trial Bank approved your loan application APP-2026-00036. The bank will contact you about next steps." |

Not exercised end to end:
* `submit_application`: A2C correctly refuses to submit without an approved consent
  request, and the trial did not build that fixture. Its call site is covered by the
  unit test only.
* Translations: none exist yet.

### DPI Consent Manager

| Check | Result |
|---|---|
| Unit tests for the delivery module (fake OAN) | **5/5 pass** |
| End to end through the real HTTP API | Partner with OTP policy → consent request → issue OTP → **SMS reaches the gateway through OAN** ("OAN: ###### is your code to allow Trial Bank to use your registry data. It expires in 5 minutes. Never share this code.") → wrong code 401 → right code verifies → consent **approved** |
| Re-issue | Two SMS with different codes. The stale code is rejected (401) and the new one works |
| Subject without a phone | **422 `otp_destination_unknown`**, nothing sent (it was a 500 before the second patch commit) |
| Logs | No OTP code appears anywhere in the Consent Manager log |

## 4. Findings

| # | Finding | Severity | Owner | Status |
|---|---|---|---|---|
| 1 | OAN log redaction emptied `record.args`, so **uvicorn's access formatter raised on every API request**: the access log was lost and a stack trace went to stderr. No secret leaked. | Medium (ops) | OAN (msg07b `redaction.py`) | **Fixed in msg08** with a regression test; to be ported to msg07b |
| 2 | Python **3.13+ verifies certificates strictly**. Frappe v16 runs 3.14, so any private CA used for OAN in dev or staging must issue certificates with Authority and Subject Key Identifiers, and CA key usage. The OAN image (3.11) did not notice. | High for staging cut-over | DevOps | Trial CA fixed; add to the runbook and certificate requirements |
| 3 | `queue_sms` could **raise** when the referenced record did not exist (`LinkValidationError`), which would break the business action | High (caller safety) | Connector | **Fixed**: the reference link is informational, the insert runs in a savepoint, and a test proves it never raises |
| 4 | The OAN client correctly refuses a non-HTTPS base URL. Dev and staging OAN **must be HTTPS**, and callers need the CA bundle if it is private (`oan_messaging_ca_bundle`) | Info / config | DevOps | Documented in the connector README |
| 5 | Receipt matching assumes **8028 `sms_id`s never repeat**. A restarted fake gateway reused an id, and OAN recorded the duplicate receipt as `recorded_no_change` (correct). Real 8028 behaviour is unconfirmed. | Medium | 8028 contract questions | Added to the open 8028 questions |
| 6 | **The Consent Manager does not build or run as checked in.** The Dockerfile pins `openg2p-fastapi-common` 1.2.0 and compose uses `develop`, but both removed `utils/crypto`. `v1.1.5` has the module but not `build_crypto_helper`; the code needs commit `9ddccc00f5` (19 July 2026) or an update to the new `crypto` package. | High (DPI) | DPI team | Trial pinned to `9ddccc00f5`; report upstream |
| 7 | The Consent Manager image lacks **`greenlet`** (SQLAlchemy asyncio), so the service cannot start | High (DPI) | DPI team | Trial-only layer adds it; fix in their dependencies |
| 8 | The Consent Manager **logged OTP codes in plaintext** at WARNING level | High (security) | DPI team | **Fixed in the patch**: codes are masked unless `otp_debug_enabled` |
| 9 | A2C's own suite has 6 pre-existing failures in `test_farmer_b2c` (unexpected internal errors; the Error Log shows missing email setup and a missing test fixture file) | Low / unrelated | A2C team | Reported |
| 10 | Frappe v16 needs **MariaDB client headers** (`mysqlclient`) in any custom bench image | Info | DevOps | Trial image fixed |
| 11 | The Consent Manager's OTP **issue** path did not translate provider errors, so a delivery failure became a bare **500** (the verify path already handled them) | Medium | DPI (patch) | **Fixed** in patch commit 0002, for both the consent and the aggregation paths |

## 5. Not covered by this trial

* Real 8028 or real Telegram: that needs a controlled live window.
* Kubernetes and Helm for the callers: OAN's own chart was proven earlier on kind.
* Translations: none exist yet.
* A2C `submit_application` end to end: it needs a consent fixture.
* The Consent Manager's aggregation (data-release) OTP over HTTP: it needs registries.
  It is covered by the same delivery module and the unit tests.
* The Consent Manager with authentication on.
* Load.

## 6. Adopting this

* **Credit:** install `oan_messaging_connector` on the site and apply
  `patches/oan_a2c_service/*.patch`. Set `oan_messaging_enabled`, `oan_messaging_base_url`,
  `oan_messaging_token` and, for a private CA, `oan_messaging_ca_bundle`. Make sure the
  scheduler and workers are running.
* **Grievance, DA:** install the same connector and replace their SMS calls with
  `queue_sms(...)`. Each keeps only its own texts and event mapping (see A2C's
  `oan_a2c/oan_messaging/`).
* **Auth:** OTPs must not go through the connector's outbox (it stores the text). Auth
  sends at once from its own post-commit job, reusing only the connector's settings and
  client (handoff guides 04 and 14).
* **Consent Manager:** apply `patches/consent-management/*.patch` and set
  `CONSENT_MANAGER_OAN_DELIVERY_ENABLED`, `..._OAN_BASE_URL`, `..._OAN_TOKEN` and
  `..._OAN_CA_BUNDLE`. **Replace the dev phone map** with a resolver approved by the
  identity owner.
* **Reproduce the trial:** see `stack/README.md`.
