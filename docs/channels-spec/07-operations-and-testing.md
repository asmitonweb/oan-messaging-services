# 7 Deployment, operations and testing

## 7.1 Environments

| Item | SMS (OAN Messaging) | Telegram |
|---|---|---|
| Namespaces | Own namespace and Helm release (`oan-messaging`) | RapidPro deployment; `oan-bot-services` release |
| Databases | Own PostgreSQL database; owner role for migrations, runtime role for processes | RapidPro's PostgreSQL and Redis; OAN Bot Services' own PostgreSQL |
| Secrets | Caller tokens are hashed in OAN; content and fingerprint keys, database URLs, 8028 client certificate and key, 8028 client secret, 8028's callback CA (and optional pinned callback certificates), callback secret: all in Kubernetes Secrets | Bot token per environment (courier channel configuration); webhook secret; `contact_ref` HMAC key; Keycloak client secret; flow ↔ service credential |
| Public exposure | 8028 receipt listener only, at the registered callback host (mutual TLS with 8028's callback CA, plus the callback secret) | One webhook path (Design 1) and the GET-only media route (§5.9) |
| URLs | Dev and staging URLs to be published by DevOps; all examples use `example.invalid` | Same |
| Certificates | HTTPS everywhere. Python 3.13 and later (including Frappe v16 on 3.14) verify strictly: private CA certificates need Subject and Authority Key Identifiers and CA key usage. Ask 8028 to confirm its certificates carry both identifiers. | Public certificate on the webhook host |

## 7.2 Network

* **OAN Messaging:** default deny. API ingress from caller namespaces only. Worker egress
  to 8028 (mutual TLS) and the database. Receipt ingress from 8028 only.
* **Courier:** ingress only from the gateway; egress to `api.telegram.org:443` and
  RapidPro internals.
* **OAN Bot Services:** egress to Master Data, Marketplace, the DA backend, the beneficiary
  API, the Auth service, Keycloak, Audit Manager, its PostgreSQL and the RapidPro API (and
  Garage). No egress to OAN Messaging. Ingress: from mailroom (flow calls), the DA backend
  (`/broadcasts`, Keycloak token) and Reports Central (chart API, credential to be agreed
  under ID-08), each on its own APISIX route.
* On the Istio mesh, allow `api.telegram.org` by hostname (ServiceEntry, registry-only
  outbound) rather than by IP range.

## 7.3 Operations

| Task | SMS | Telegram |
|---|---|---|
| Health | `/livez`, `/readyz`, worker heartbeat probe | Courier and mailroom health; `getWebhookInfo`; OAN Bot Services health |
| Alerts | `status-check` CronJob (`status --fail-on-alert`) | Rejected webhook calls, webhook errors, adapter failures, questions waiting for a reply, 429 pauses (Rancher monitoring) |
| Pause | `suspend --channel sms`, then `resume` | Stop the webhook (unregister or block at the gateway); updates queue at Telegram for up to 24 hours |
| Credentials | `issue-credential`, `list-credentials`, `revoke-credentials` | Rotate the bot token and webhook secret (runbook) |
| Audit | Message metadata and receipts in OAN | Audit Manager events for every question created, delivered, replied, escalated and closed, and for every broadcast accepted (sender, scope, approval, recipient count) |
| Restore | PostgreSQL backups; the cleanup sweeper resumes on recovery | RapidPro and OAN Bot Services backups |

**Rollout across versions (open):** the OAN Messaging chart runs migrations before an
upgrade, but there is no phased compatibility matrix yet for upgrades that change
behaviour across versions (release blocker OI-72).

## 7.4 Security checklist (both channels, before UAT)

* Secrets only in Kubernetes Secrets or a secret manager; none in values, images or git;
  separate credentials per environment.
* Service accounts per environment with only the routes each needs; never test or admin
  accounts.
* Containers non-root, read-only file system, requests and limits set.
* Logs carry no message text, phone numbers, Farmer IDs, OTP codes, names, tokens or media
  links. OAN Messaging redacts at the logging-handler level; the bot logs only
  `contact_ref`.
* RapidPro data under the configured retention (RP-D1, 90 days by default), database and
  media storage encrypted, operator-only access.
* Rate limits per contact in the bot (for example 20 actions a minute).
* Media: attachments in Garage under RP-D1 retention, exposed only through the GET-only
  public media route (§5.9); for Option B, Garage with encryption, retention and signed
  links, scanned before a Development Agent opens them.

## 7.5 Testing and Definition of Done

A module is done when its acceptance criteria pass in Development, in both languages,
and these checks are green.

| Test | SMS | Telegram |
|---|---|---|
| Unit and integration | OAN suite against local PostgreSQL with mocked providers | Every flow path in both languages, including unexpected replies, Back, Main menu and timeouts |
| Contract | Each caller against `openapi.yaml`; connector and client tests | Each OAN Bot Services adapter against its agreed JSON contract; flow ↔ service contract tests |
| Provider | Mocked 8028 (mutual TLS, receipts) | A test bot: keyboards, media, 429 pause, blocked user |
| Security | Secret scan, image scan, log check | Webhook: requests without the secret or from other addresses rejected; other RapidPro paths unreachable; `getWebhookInfo` clean |
| Linking | Auth OTP sent to the registered phone through OAN | Same and alternate number; someone else's contact refused; unknown ID; wrong, expired and repeated codes; lockout; unlink; registered phone changed; alternate never reaches the registry, SMS senders or logs |
| Load | Rate limit held across workers; batches of 100 identical texts; a broadcast is never starved under saturating transactional traffic (ageing) | 500 simulated farmers for 10 minutes: no lost messages, replies ahead of reminders. **Region-wide Emergency:** one Emergency broadcast per woreda of the largest region (300+), started within minutes, up to 1 million linked farmers in total, with normal Q&A running: replies stay under 5 seconds, every woreda makes progress, the pace never exceeds the setting or Telegram's 30 a second, RapidPro's API and courier queue keep up, each woreda's SMS copy starts when its Telegram part ends; total time recorded |
| Language | Every template fits one SMS part with the longest real values | A native Amharic speaker reviews every string and button length |
| UAT | Controlled live acceptance with real 8028 and registered test handsets | ATI and selected Development Agents run the farmer journeys |

### Current state (15 November 2026)

| Area | Result |
|---|---|
| OAN Messaging full suite | 896 passed, 1 expected failure (broadcast fairness, disabled in the pilot). Passes on Python 3.13 after the test-certificate fix; passed on 3.12 before it. |
| Helm chart | Lint, render and schema checks passed; kind install, recovery and upgrade passed. NetworkPolicy and LoadBalancer behaviour not tested. |
| Integration trial | Credit (9 messages, each delivered once) and Consent Manager OTP passed end to end against a fake 8028. |
| Release blockers | 16 listed: all fixed pending the independent review (20 Nov 2026), one with a known limit (redaction of direct stream writes). The go-live checklist (environments, 8028 credentials and live test, load, security, UAT) is in `docs/evidence/RELEASE-BLOCKERS-STATUS.md`. |
| Telegram | Nothing built yet. |
| Live | No real 8028 or Telegram traffic yet. |

## 7.6 RapidPro release gate (RV-01)

Run on the deployed RapidPro version before go-live, and again after every RapidPro
upgrade. Owners: delivery team, ATI DevOps and security. Telegram does not go live until
every check passes.

* Stop deletes the contact, staff-side message copies and media
* Retention deletes messages, media and archives
* Webhook log retention
* Garage uploads with the deployed AWS SDK (checksums)
* Telegram fetches media through the public route
* Relayed media never carries Telegram's file URL with the bot token
* Flow-start skip and interrupt behaviour
* Replies ahead of broadcasts at the configured rate
* The HTTP log retention setting covers webhook logs
* A broadcast arriving mid-flow keeps the farmer's open keyboard usable
* Media links stop after 7 days
* The region-wide Emergency load test (§7.5)
