# OAN Messaging Service — Architecture (contract 3.0.0)

Status (refreshed 15 November 2026): **Review.** This copy describes the msg08 working
folder: contract 3.0.0 (unchanged) plus the reliability work in msg07b (not yet
accepted) and the runtime and operations work in MSG-08 (not yet accepted). Neither is
in canonical. The external contract has not changed since 3.0.0; everything in the new
section [Reliability and operations](#reliability-and-operations-msg07b-and-msg08) is
internal to the service.

The service sends outbound SMS (through the 8028 gateway) and Telegram messages for
other OAN services. It accepts requests durably, sends them from background workers,
and reports honest outcomes. It does not own OTP generation, identity validation,
inbound SMS, Telegram conversations, registry SMS, Novu or deployment.

Frozen interfaces: [`oan_messaging/contracts.py`](../oan_messaging/contracts.py) (Python) and
[`docs/openapi.yaml`](openapi.yaml) (HTTP). 8028 specifics: [`docs/8028-contract.md`](8028-contract.md).

## Changes in 3.1.0 and 3.2.0 (20 November 2026)

| Version | Change |
|---|---|
| 3.1.0 | SMS of up to 3 parts (SMS-09); an OTP stays one part (`too_long_for_sms`, `too_long_for_single_sms`). Broadcast ageing (SMS-03, migration `0004`). 8028 paced at 4 SMS/s with one OTP slot (SMS-10); a 500 from 8028 is retried (SMS-11); receipts read `status` only (SMS-07) |
| 3.2.0 | **Statistics:** `GET /api/v1/charts/{chartId}` and `GET /api/v1/charts?charts=`, the dashboard service contract of the OAN dashboards, for statistics readers only (see [Statistics](#statistics-get-apiv1charts)). Migration `0005`: snapshot tables, `callers.can_read_stats`, `ix_messages_created_at` |

## Changes in 3.0.0 (from the review of 2.0.0)

| # | Change |
|---|---|
| P1 | Idempotency bindings are no longer released by age. Bindings of `accepted`, `sending` and `unknown` messages are kept however old (`OUTSTANDING_BINDING_STATUSES`). Only completed messages (`sent`, `delivered`, `failed`, `expired`) may be released, and only after the guarantee window (`binding_release_due`). `classify_idempotent_request` no longer takes a clock. Key retirement now depends on live verification needs (`can_retire_key_version`, `fingerprint_key_versions_in_use`), not on time since rotation. |
| P2 | Released bindings are representable. `StoredMessage.payload_fingerprint` is optional and `binding_released_at` was added; a live binding must have a fingerprint, and a released one must not. GET (`to_status_view`) still works after release. Receipt matching is anchored on `sent_at`, else on the new `last_attempt_at`, else on `created_at` (`receipt_anchor`, `recipient_match_deadline`). The 7-minute cleanup is now a target that assumes workers and the database are up; cleanup resumes on recovery. |
| P2 | Every `ServicePolicy` duration must be strictly positive, on top of the existing cross-field checks. |
| Q2 | Example keys are opaque UUIDs. Keys and `source_reference` must not contain OTPs, phone numbers or other personal data. |
| Q3 | Callers must never recycle keys. A retry beyond the guarantee window must reconcile through GET instead of resubmitting. |
| Q4 | Text stays byte-exact; retries must preserve the original text and `expires_at`. |

## Changes in 2.0.0 (from the review of 1.0.0)

| # | Change |
|---|---|
| P1-1 | Required `Idempotency-Key` header, scoped to (caller, channel), separate from `source_reference`. `source_reference` is now non-unique business correlation. |
| P1-2 | Deduplication and receipt matching use retained, versioned, keyed HMAC-SHA256 fingerprints (`canonical_payload`, `keyed_fingerprint`, `classify_idempotent_request`). Raw callback phone numbers are never persisted. |
| P1-3 | Content is purged as soon as a message leaves `accepted`/`sending`, **including `sent`**, with an inline purge plus a bounded, crash-safe sweeper (`purge_due`). |
| P2-4 | Telegram status views must have `delivery_receipts_supported=false` and can never be `delivered`; SMS views must be `true`. Enforced in the model and the OpenAPI schema. Receipts are 8028-only. |
| P2-5 | Uncertain API outcomes (timeout, dropped connection, any 5xx including 503) are retried by the caller with the same key and body. No promise that 503 means "not stored". |
| — | `handset_unreachable` renamed `provider_delivery_expired` (the receipt proves expiry, not cause). Policy limits are a configurable `ServicePolicy`. |

## Reliability and operations (msg07b and msg08)

Added after 3.0.0. None of it changes the HTTP contract.

| Area | Behaviour | Where |
|---|---|---|
| Provider faults | A refused credential or TLS configuration **suspends** the channel instead of failing the backlog. Repeated ambiguous outcomes start a 60-second **cooldown**, and a provider `retry_after` sets one too. A late success never resumes a suspended channel; an operator does, with `resume`. | `dispatch_controls` table (migration 0003), `storage/repository.py` |
| Undecryptable rows | A row whose content cannot be decrypted (for example a missing key version) is **deferred**: content kept, error logged, other work continues. It is never failed or purged for that reason. | `storage/repository.py` |
| Late provider success | Success evidence is fenced to the exact attempt, so a late `sms_id` is kept and later receipts still match. | `storage/repository.py` |
| Claims | Batch size 1 by default, so an OTP never waits behind a claimed batch. A worker claims only the channels it can send. | `worker/dispatcher.py`, `claim_due` |
| Rate limit (8028: 4 SMS/s, configurable) | The reservation runs **inside** the `begin_attempt` transaction: a transaction-scoped advisory lock, the time sample and start count in one statement, and the state change plus attempt insert in one CTE. Default tolerance 200 ms (about 14.3 calls/s). A cancelled reservation can never leave the lock held. | `worker/rate_limit.py`, migration 0002 (start-time index) |
| Start-up guards | The worker refuses to start if measured database latency (`4 × p95 + 5 ms`) exceeds half the tolerance, if a key version referenced by stored data is missing, or if an enabled Telegram caller's bot has no worker token. | `runtime/__main__.py` |
| Transient database errors | The worker loop recovers instead of exiting. | `worker/dispatcher.py` |
| Liveness | The worker writes a heartbeat each cycle; `check-heartbeat` is its exec probe. The API has `/livez` and `/readyz`. | `runtime/` |
| Alerts | `status [--fail-on-alert]` (exit 2 on alert), run by the Helm `status-check` CronJob. Alerts: queue age, stale sending, unknown outcomes, late starts, deferred dispatch, receipt anomalies, provider suspended. No metrics endpoint, by decision. | `runtime/ops.py` |
| Operator commands | `caller` (refuses `broadcast` unless broadcasts are enabled), `issue-credential`, `list-credentials`, `revoke-credentials --credential ID \| --all`, `suspend`, `resume`, `cleanup [--forever]`. | `runtime/__main__.py`, [runbook](runbook.md) |
| Least privilege | Migrations run as the owner role; API, worker, receipts and cleanup run as a runtime role with explicit grants. | `deploy/sql/grants.sql` |
| Redaction | Applied at the handler level, so loggers and handlers added later are covered; uvicorn access lines keep their (redacted) arguments. Direct stream writes and third-party exporters are not covered. | `redaction.py` |
| Packaging | One image (non-root uid 10001, hash-pinned dependencies) runs `api`, `worker`, `receipts` and `cleanup`; Helm chart with a pre-install/pre-upgrade migration hook and `values-pilot.yaml`. | `Dockerfile`, `deploy/helm/oan-messaging`, [Helm guide](deployment/helm.md) |

**Broadcast:** off unless `OAN_MSG_BROADCAST_ENABLED=true`. Broadcast ageing (SMS-03) keeps
it from starving under higher-priority load (see [Priority and rate limits](#priority-and-rate-limits)). Telegram is implemented, but Phase 1 has no
Telegram caller: the OAN Telegram bot runs on RapidPro and sends to Telegram itself.

## Statistics (GET /api/v1/charts)

Modelled on the other OAN dashboard services (A2C `oan_a2c/api/v1/dashboard.py`,
Grievance), so Reports Central reads it as one more upstream:

* **Routes:** `GET /api/v1/charts/{chartId}` returns `{"success": true, "data": [rows],
  "meta": {"as_of", "chart", "from", "to", "granularity"}}`; `GET /api/v1/charts?charts=a,b`
  returns `data: {chartId: {"chartName", "data"}}` (at most 20).
* **Access:** a **statistics reader** only: a caller created with `caller --stats-reader`
  (no channels, no purposes, `can_read_stats`), whose token is issued as any caller's. A
  sending caller gets `403 stats_not_permitted`; a reader cannot send.
* **Data:** counts only, never a recipient, text or reference. The cleanup component
  rebuilds a snapshot (`stats_daily`, `stats_failures`, `stats_campaigns`, `stats_state`)
  every `OAN_MSG_STATS_REFRESH_SECONDS` (900) under an advisory lock, rebuilding the last
  8 days (statuses change for up to the 7-day receipt window); older days are final. A
  message counts on the Ethiopian day (Africa/Addis_Ababa) it was accepted, with its
  current status. `python -m oan_messaging.runtime stats-refresh` runs one rebuild.
* **Charts:** `msgKpis`, `msgTrend`, `msgByCaller`, `msgByPurpose`, `msgFailureReasons`,
  `msgOtpLatency`, `msgCampaigns` (alert and broadcast messages sharing the first two parts
  of `source_reference`, two or more recipients) from the snapshot; `msgQueueNow`,
  `msgProviderHealth` and `msgFilterCallers` live.
* **Filters:** `from`, `to` (default the last 30 days, at most 366), `granularity`
  (day, week, month), `caller`, `purpose`, `channel`; other parameters are ignored.
* **Latency:** median and p95 from histograms (buckets 1 s … 1 day), reported as the bucket's
  upper edge in seconds.

## Stack

Python ≥ 3.11, FastAPI, Pydantic v2, PostgreSQL, SQLAlchemy 2 (asyncio, psycopg 3),
Alembic, httpx. Tests: pytest, pytest-asyncio, respx (mocked HTTP). No real network
calls in tests or local runs.

## Components

```
 caller service ──HTTPS + bearer + Idempotency-Key──▶ API (FastAPI)          MSG-02
                                      │ validate, authorize, dedupe, persist, commit
                                      ▼
                                   PostgreSQL  ◀───────────── Receipts ingress   MSG-05
                                      ▲  messages, content (encrypted),  ▲ (verified mTLS,
                                      │  attempts, receipts, callers      │  persist then 200)
                                      ▼                                   │
                                   Worker(s) + purge sweeper         8028 status callback
                                      │ claim (SKIP LOCKED, lease), rate limit,
                                      │ re-check expiry, commit attempt, call adapter
                                      ▼
                         MessageAdapter.send()  ──▶ 8028 Send SMS API   MSG-03
                                                ──▶ Telegram Bot API    MSG-04
```

| Module (planned path) | Owner | Uses |
|---|---|---|
| `oan_messaging/contracts.py` | MSG-01 | — |
| `oan_messaging/api/`, `oan_messaging/storage/`, `migrations/` | MSG-02 | submissions, responses, `Problem`, `CallerContext`, `MessageRepository`, `Fingerprinter`, `canonical_payload`, `classify_idempotent_request`, `check_expiry`, `ServicePolicy`, `ALLOWED_TRANSITIONS`, `PURGEABLE_STATUSES` |
| `oan_messaging/adapters/sms8028.py` | MSG-03 | `MessageAdapter`, `SmsSendRequest`, `ProviderResult`, `sms_length_units` |
| `oan_messaging/adapters/telegram.py` | MSG-04 | `MessageAdapter`, `TelegramSendRequest`, `ProviderResult` |
| `oan_messaging/worker/`, `oan_messaging/receipts/` | MSG-05 | `MessageRepository`, `Claim`, `OUTCOME_TO_STATUS`, `DeliveryReceipt`, `StoredReceipt`, `RECEIPT_STATUS_MAP`, `PURPOSE_PRIORITY`, `ServicePolicy` |
| `oan_messaging/runtime/`, `deploy/`, `Dockerfile` | MSG-08 | process entry points, operator commands, status and heartbeat, grants, Helm chart |

## HTTP API

All endpoints require `Authorization: Bearer <service credential>`. Each calling service
gets its own credential (MSG-02), which resolves to a `CallerContext`: caller id, allowed
channels, allowed purposes, and for Telegram the configured bot key.

| Endpoint | Success | Notes |
|---|---|---|
| `POST /api/v1/messages/sms` | 202 new, 200 identical replay | `Idempotency-Key` header; one Ethiopian mobile recipient; single-part text |
| `POST /api/v1/messages/telegram` | 202 new, 200 identical replay | `Idempotency-Key` header; known `chat_id`, plain text; bot from caller config |
| `GET /api/v1/messages/{id}` | 200 | 404 for unknown **and** other callers' ids |

Errors use `application/problem+json` (`Problem`) with field locations and codes only —
no submitted values.

### Submission fields

| Field | SMS | Telegram | Rule |
|---|---|---|---|
| `Idempotency-Key` (header) | required | required | `^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$`; one per intended delivery |
| `recipient` | required | — | `09XXXXXXXX`, `07XXXXXXXX`, `2519…`, `+2519…`, `+2517…`; stored as E.164 |
| `chat_id` | — | required | non-zero integer, \|id\| < 2^52; recipient must have started the bot |
| `text` | required | required | not blank, no control chars except `\n` `\r`. SMS: up to 3 parts (one part 160 GSM-7 septets / 70 UTF-16 units; longer texts 153 / 67 per part, at most 459 / 201); an OTP is one part. Telegram: ≤ 4096 UTF-16 units, no `parse_mode` |
| `source_reference` | required | required | `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; business correlation, **not unique** |
| `purpose` | required | required | `otp`, `transactional`, `alert`, `broadcast`; caller must be permitted |
| `expires_at` | optional | optional | RFC 3339 with offset. **Required for `otp`.** Must be in the future and within the policy ceiling (defaults: OTP 30 min, others 7 days) |

Unknown fields are rejected (`additionalProperties: false`), so a request can never carry
a bot token, provider URL, priority or a body-level idempotency key.

## Idempotency

**Key vs. reference.** `Idempotency-Key` identifies one intended delivery;
`source_reference` identifies the caller's business event. One grievance update sent to
three farmers is three deliveries: same `source_reference`, three keys. Callers create
one opaque key per delivery (a random UUID stored with the delivery, or another opaque
stable identifier) and reuse it on every retry. Keys and `source_reference` must never
contain OTPs, phone numbers or other personal data. Keys are never recycled.

**Scope.** `(caller_id, channel, Idempotency-Key)`, unique among rows whose binding is
live (partial unique index `WHERE binding_released_at IS NULL`).

**Canonical payload** (`canonical_payload`, version 1): JSON with sorted keys and no
whitespace over `caller_id`, `channel`, `purpose`, `source_reference`, normalized E.164
`recipient` or `chat_id`, byte-exact `text` (no Unicode normalization) and `expires_at`
in UTC with microseconds. `0912000001` and `+251912000001` canonicalize the same, as do
equivalent instants written with different offsets. Text is not Unicode-normalized:
retries must resend the original text and `expires_at` exactly.

**Decision** (`classify_idempotent_request`), evaluated **before** any time-sensitive
validation:

| Situation | Decision | HTTP |
|---|---|---|
| No row, or the row's binding was released | `new` → `check_expiry`, persist, commit | 202 |
| Binding exists, payload fingerprint equal | `replay` → original message, current status | 200 |
| Binding exists, payload fingerprint differs | `conflict` | 409 |

Replay works after content purge and after the original `expires_at`: only the retained
payload fingerprint is compared, and expiry is validated only for new acceptances.
Concurrent identical requests must produce one row (unique constraint); the losing
transaction re-reads and classifies.

The decision never depends on age. A live binding always returns `replay` or `conflict`.

**Binding retention** (`binding_release_due`):

| Message status | Binding + payload fingerprint |
|---|---|
| `accepted`, `sending`, `unknown` (outstanding) | **kept indefinitely.** Releasing it could create a second copy while the first is still queued (non-OTP messages may have no expiry), or repeat a delivery whose outcome is unproven. |
| `sent`, `delivered`, `failed`, `expired` (completed: no further provider submission possible) | kept for at least `dedup_window` (default 30 days, ≥ expiry ceiling + 1 day) from `created_at`, then may be released |

`sent` is completed for binding purposes even without a final receipt: the provider took
the message and nothing more will be sent. `unknown` is not completed: it is unproven,
not sent.

**Release representation.** `purge_due` sets `binding_released_at`, clears
`payload_fingerprint` and keeps the row, so `GET` works unchanged. A row with no
fingerprint and no `binding_released_at` is invalid. Storage must treat it as an
integrity error, never as "new".

**Beyond the window.** A retry after a completed message's binding was released is not
automatically safe: it would be accepted as a new delivery. Clients that lose track of a
delivery for that long must reconcile through `GET /api/v1/messages/{id}` (or
out-of-band) instead of resubmitting, and must never switch to a new key for the same
delivery.

### Retries: API replay vs. gateway resend

* **API replay — safe.** A commit can succeed while its response is lost. On a timeout,
  dropped connection or any 5xx (including 503) the caller cannot know whether the
  message was stored, and must retry with the same key and unchanged body. The result is
  the original message (200) or a single new acceptance (202). A changed body is 409.
* **Gateway resend — never blind.** An ambiguous provider call leaves the message
  `unknown`; the worker does not resend it.

## Keyed fingerprints

A plain hash of a phone number or a 6-digit OTP is brute-forceable, so fingerprints are
HMAC-SHA256 under a secret key (`keyed_fingerprint`):

* input `oan-msg/<domain>/v1 || 0x00 || data`, domains `payload` and `recipient`;
* keys ≥ 32 random bytes from secret configuration (never in the database), identified by
  a key version;
* every row stores the key version it used; new rows use the current version;
  verification recomputes under the row's version (`KeyedFingerprint.matches` refuses
  cross-version comparison);
* after rotation, an old key stays verification-only until no retained fingerprint uses
  it. `fingerprint_key_versions_in_use` reports versions referenced by live-binding payload
  fingerprints, unreleased recipient fingerprints and stored receipt fingerprints;
  `can_retire_key_version` allows destroying a non-current version only if it is absent
  from that set. Time since rotation alone is never enough: an `unknown` binding can keep
  a key alive indefinitely.

| Fingerprint | Stored on | Purpose | Retained until |
|---|---|---|---|
| payload | message row | idempotency equality | outstanding: indefinitely; completed: until released after `created_at + dedup_window` |
| recipient (SMS) | message row | 8028 receipt matching | `recipient_match_deadline` = anchor + `receipt_window` (default 7 days); anchor = `sent_at`, else `last_attempt_at`, else `created_at`; kept while `accepted`/`sending` |
| recipient (SMS) | receipt row | evidence, orphan re-match | `received_at + receipt_evidence_retention` (default 90 days) |

## Message lifecycle

```
accepted ──▶ sending ──▶ sent ──▶ delivered        (final, SMS only)
   │            │          └────▶ failed           (final)
   │            ├──▶ failed                         (final, definite rejection)
   │            ├──▶ unknown ──▶ sent | delivered | failed   (only via provider evidence)
   │            ├──▶ accepted  (NOT_SENT + retryable: provably never reached provider)
   │            └──▶ expired   (NOT_SENT and expiry passed)
   └──▶ expired                                     (final, never left the service)
```

`ALLOWED_TRANSITIONS` in `contracts.py` is the source of truth. Anything else — including
a `Sent` receipt after `Delivered`, or any receipt for a final message — is recorded but
does not change status.

| Status | Evidence required | Handset claim |
|---|---|---|
| `accepted` | committed row | no |
| `sending` | committed attempt row, before the network call | no |
| `sent` | provider success body with a provider id (8028 `sms_id`, Telegram `message_id`) | **no** |
| `delivered` | authenticated 8028 `Delivered` callback matched on `sms_id` **and** recipient fingerprint | yes (operator report) |
| `failed` | definite rejection or final negative receipt (`Rejected`, `Expired`); raw provider status kept | — |
| `expired` | `expires_at` reached before any attempt was committed | — |
| `unknown` | attempt committed, outcome unprovable | unknown |

`delivery_receipts_supported` is fixed per channel (`CHANNEL_DELIVERY_RECEIPTS`): `true`
for SMS, `false` for Telegram. A Telegram view with `true`, or a Telegram view that is
`delivered` or has `delivered_at`, is invalid in both the model and the OpenAPI schema.
No outcome is an exactly-once handset guarantee.

## Content retention and purge

Content = message text plus recipient/chat ciphertext (encrypted at rest, MSG-02).

* Content is needed only in `CONTENT_REQUIRED_STATUSES` = {`accepted`, `sending`}.
  `sending → accepted` (provably not sent, retryable) keeps it for the retry.
* Every transaction that moves a message into `PURGEABLE_STATUSES` (`sent`, `delivered`,
  `failed`, `expired`, `unknown`) deletes its content **in the same transaction**. This
  includes `sent`: Telegram `sent` is never final and SMS `sent` may never get a receipt,
  so purge is independent of receipt finality.
* `finish_attempt` re-matches orphan receipts before purging, while the recipient is
  still available.
* **Crash-safe backstop.** `purge_due` runs every `purge_sweep_interval` (default 5 min)
  and removes content from any purgeable row that still has it; `recover_stale` moves
  stuck `sending` rows to `unknown` and purges them. Target: content is gone within
  `worker_lease + purge_sweep_interval` (default 7 min) of the send outcome **while workers
  and the database are available**. An outage cannot meet an absolute wall-clock
  deadline: the sweeper resumes on recovery and processes the backlog first.
* After purge the row keeps only metadata: ids, key, `source_reference`, status, provider
  id/status, masked recipient, timestamps, key versions and fingerprints.
* `purge_due` also releases recipient fingerprints at `recipient_match_deadline`,
  completed bindings where `binding_release_due`, and receipt evidence after
  `receipt_evidence_retention`. It never releases an outstanding binding.
* Releasing an `unknown` SMS message's recipient fingerprint only stops late receipt
  matching (those receipts become `unmatched_late`). The message stays `unknown`, keeps
  its binding and is never resent.

## Service policy (configurable defaults)

`ServicePolicy` holds OAN service limits, loaded from configuration (MSG-02/05). These
are not identity-system rules: the OTP ceiling does not validate an identity provider's
OTP lifetime. OAN never extends, defaults or rewrites a caller's `expires_at`; it only
rejects values outside the ceilings.

| Setting | Default | Constraint |
|---|---|---|
| `max_otp_ttl` | 30 min | ≤ `max_expiry_horizon` |
| `max_expiry_horizon` | 7 days | — |
| `dedup_window` | 30 days | ≥ `max_expiry_horizon` + 1 day; minimum guarantee for completed bindings |
| `receipt_window` | 7 days after the receipt anchor | — |
| `receipt_evidence_retention` | 90 days | ≥ `receipt_window` |
| `purge_sweep_interval` | 5 min | — |
| `worker_lease` | 2 min | — |

Every duration must be strictly positive. Zero or negative values are rejected, so a
misconfiguration can never silently disable delivery or reconciliation. These are
configurable initial defaults, not provider guarantees and not an approved production
retention policy.

## Adapter contract

```python
class MessageAdapter(Protocol):
    provider: ClassVar[Provider]
    channel: ClassVar[Channel]
    async def send(self, request: SendRequest) -> ProviderResult: ...
```

* One provider call per `send`; no internal retries.
* Provider and network failures are **returned**, not raised. Anything raised is treated
  as `UNKNOWN`/`interrupted` because the request may have reached the provider.
* `ProviderResult.outcome`:
  * `sent`: the provider took it; `provider_message_id` is required.
  * `rejected`: a definite refusal (e.g. 8028 400 invalid number; Telegram chat not found /
    bot blocked). → `failed`.
  * `not_sent`: provably never reached the provider (connection refused, TLS handshake
    failure, Telegram 429 with `retry_after`). `retryable=false` for credential/config
    problems so the worker does not loop. → back to `accepted` (or `failed`/`expired`).
  * `unknown`: a timeout after the request may have been sent, 5xx, or a malformed or partial
    success body. → `unknown`. **Never retryable.**
* `provider_status` keeps the raw provider signal as a short token (`http_503`, `tg_403`,
  `timeout`); `detail` is ≤ 200 chars. Neither may contain secrets, tokens, bodies, text or
  recipients.
* Adapters get the recipient in E.164; the 8028 adapter converts to domestic `09/07`.
  The Telegram adapter receives a configured `bot` key, never a token or URL.

## Persistence contract

`MessageRepository` (implemented in `storage/` by MSG-02, driven by MSG-05). Every method
takes the current time as a parameter so tests can use a fake clock.

| Method | Contract |
|---|---|
| `create_or_get(caller, idempotency_key, submission, now)` | Look up the **live** binding first (no age-based release); `replay` returns the original, `conflict` raises `IdempotencyConflict`, `new` runs `check_expiry`, computes fingerprints and commits before 202. Returns `(message, decision)`. |
| `get_for_caller` | Caller-scoped; `None` for others' ids. |
| `claim_due` | `FOR UPDATE SKIP LOCKED`, order by `(priority, created_at)`, honor `not_before`, bounded lease; expired rows → `expired` (purged), not claimed. |
| `begin_attempt` | Commit `accepted → sending`, an attempt row and `last_attempt_at` **before** the network call; raises `LeaseLost`. |
| `finish_attempt` | Apply `OUTCOME_TO_STATUS`, store provider id/status, re-match orphan receipts, purge content if purgeable, schedule retry via `not_before`. |
| `mark_expired` | `→ expired` / `expired_before_send`, purged in the same transaction. |
| `recover_stale` | `sending` with expired lease → `unknown` / `interrupted`, purged. |
| `record_receipt(receipt, now)` | Always persist a `StoredReceipt` (fingerprint + masked phone, never the raw number); apply only if `sms_id` and recipient fingerprint both match before `recipient_match_deadline` and the transition is legal. Returns `applied`, `recorded_no_change`, `orphaned`, `mismatched` or `unmatched_late`. Commit before the callback returns 200. |
| `purge_due(now, limit)` | Crash-safe backstop and retention releases (see above). Never touches outstanding bindings. Idempotent; resumes after outages. |
| `fingerprint_key_versions_in_use(now)` | Key versions still referenced by retained fingerprints; input to `can_retire_key_version`. |

Logs and errors never include text, full recipients, tokens or secrets.

## Priority and rate limits

`PURPOSE_PRIORITY`: `otp` 0 < `transactional` 10 < `alert` 20 < `broadcast` 30 (lower is
claimed first). **Broadcast ageing (SMS-03):** the worker claims OTPs first; then every other
message accepted at least `broadcast_max_wait` ago (`OAN_MSG_BROADCAST_MAX_WAIT_SECONDS`,
default 180), oldest first whatever its purpose; then the rest by priority and age. So an aged
broadcast goes ahead of newer transactional and alert SMS, an older alert keeps its place
ahead of it, and a waiting broadcast is never starved. Each pass follows an index
(`ix_messages_claimable`, `ix_messages_claimable_by_age`). Provider rate limits (8028: 4 SMS/s by default, `OAN_MSG_SMS_RATE_PER_SECOND`, one slot kept for OTP; the operator sends 2–5 SMS/s and 8028 queues the rest) are enforced across workers inside
the `begin_attempt` transaction (see [Reliability and operations](#reliability-and-operations-msg07b-and-msg08)).
Expiry is re-checked immediately before every network call.

## Receipts (8028 only)

Telegram has no receipt integration in this scope. 8028 calls the receipt listener at the
registered path (default `/sms-status-receiver/v1/update-status`, 8028 guide v1.1). Two
factors are checked in the process, never from a header: mutual TLS (8028's callback CA,
optionally narrowed by pinned certificates) and the callback secret in the body
(constant-time; never stored or logged). The callback is authenticated and its evidence
durably persisted before `200 {"success": true}`; matching and status changes may follow
asynchronously. The status is read from `status` (confirmed by 8028); `sms_id` may be
a number; the phone arrives as `251…`. Documented statuses map via `RECEIPT_STATUS_MAP`;
the raw status is always kept:

| 8028 `status` | Message status | Final |
|---|---|---|
| `Pending` | `sent` | no |
| `Sent` (earlier guide) | `sent` | no |
| `Delivered` | `delivered` | yes |
| `Expired` (earlier guide) | `failed` (`provider_delivery_expired`; cause not proven) | yes |
| `Rejected` | `failed` (`provider_rejected`) | yes |
| anything else, including null (`null`) or empty (`empty`) | no change, recorded and logged | no |

`event_time` is EAT (UTC+3) and converted to an aware datetime. Details and open items:
`docs/8028-contract.md`.

## Mandatory downstream acceptance scenarios

**Status (15 November 2026):** PostgreSQL, the API and workers now exist, and the full
suite (896 tests; see `docs/test-report.md`, kept with the review records and not part of
developer exports) covers this
behaviour. Only A1–A5, A8, A15 and A16 are labelled by ID in the tests; the rest have not
been mapped scenario by scenario. That mapping is an open review task. The original text
follows.

These need PostgreSQL, the API and workers (A1–A21). MSG-01 does **not**
claim they pass. Its self-check only simulates the decision rules in memory. MSG-02 and
MSG-05 must demonstrate each scenario against real PostgreSQL with a fake clock and fake
gateways:

| # | Scenario | Owner | Expected |
|---|---|---|---|
| A1 | Same key, identical body, N concurrent requests | MSG-02 | exactly one row; one 202, the rest 200 with the same id |
| A2 | Two keys, same `source_reference`, different recipients | MSG-02 | two messages, both 202 |
| A3 | Identical replay after content purge | MSG-02 | 200, original id |
| A4 | Identical replay after the original `expires_at` | MSG-02 | 200, original id (no 422) |
| A5 | Changed body (text or recipient) under same key after purge | MSG-02 | 409 |
| A6 | Completed (`sent`/`delivered`/`failed`/`expired`) message past `dedup_window`, then `purge_due`, then same key | MSG-02 | `binding_released_at` set, fingerprint cleared, row kept; GET still 200; resubmission treated as new |
| A7 | Commit succeeds, response lost (simulated), same-key retry | MSG-02 | 200, original id; one row |
| A8 | Retry with new body after uncertain response | MSG-02 | 409 |
| A9 | Key rotation: replay of a row fingerprinted under the old version | MSG-02 | 200 using the row's key version |
| A9b | Key retirement while an old-version `unknown` binding or pending recipient fingerprint exists | MSG-02 | old version reported by `fingerprint_key_versions_in_use`; retirement refused |
| A10 | 8028 receipt matched after content purge | MSG-05 | `delivered`, matched via recipient fingerprint |
| A11 | Receipt with right `sms_id`, wrong number | MSG-05 | `mismatched`, no status change |
| A12 | Receipt before send response stored (orphan), then `finish_attempt` | MSG-05 | orphan re-matched, then content purged |
| A13 | Stored receipts contain no raw phone number | MSG-05 | DB inspection finds none |
| A14 | Crash after `sent` commit but before inline purge (or purge skipped) | MSG-05 | `purge_due` removes content within `worker_lease + purge_sweep_interval` |
| A15 | Telegram `sent` and SMS `sent` with no receipt | MSG-05 | content purged; status stays `sent` |
| A16 | Crash mid-call (`sending` past lease) | MSG-05 | `recover_stale` → `unknown`, purged, never resent |
| A17 | `accepted` (no expiry), `sending` and `unknown` messages 30+ days old, then `purge_due`, then same-key submission | MSG-02/05 | bindings kept; identical body → 200 same id; changed body → 409; never a second row |
| A18 | Row with neither payload fingerprint nor `binding_released_at` (corruption) | MSG-02 | rejected as an integrity error; never accepted as new |
| A19 | `unknown`/rejected attempt without `sent_at`: receipt before and after `last_attempt_at + receipt_window` | MSG-05 | matched before the deadline; `unmatched_late` after; `unknown` never resent |
| A20 | Sweeper and workers down past the 7-min target, then restored | MSG-05 | cleanup resumes and clears the backlog; nothing skipped |
| A21 | Policy loaded with a zero or negative duration | MSG-02/05 | startup fails; no default substituted silently |

## Explicitly out of scope

Deployment, live SMS, eSignet changes or identity validation, registry SMS, Novu, sandbox
activation, inbound SMS, Telegram onboarding/conversations or receipts, multi-part SMS,
scheduled sends (`pushdatetime` in the future), multi-recipient requests.
