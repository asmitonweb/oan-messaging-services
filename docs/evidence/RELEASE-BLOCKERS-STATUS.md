# Release blockers and go-live checklist (20 November 2026)

Re-checked against `main` (contract 3.2.0, migrations up to `0005`). "Fixed" means the code
and a test exist; nothing is accepted until the independent review passes. Full suite: 919
passed on Python 3.12 (PostgreSQL 16); chart regressions passed.

## The 16 original release blockers

| ID | Blocker | Status | Evidence |
|---|---|---|---|
| OI-07 | Dispatch timing under database latency (F-03) | **Fixed** | Reservation inside `begin_attempt`; start-up latency guard; `tests/worker/test_dispatch_latency.py` (hardened 19 Nov: three rounds, hard guarantees every round) |
| OI-08 | Credential/TLS fault fails the whole backlog (F-04) | **Fixed** | `dispatch_controls` suspend and cooldown (`0003`); `suspend`/`resume`; `suspended:<channel>` alert |
| OI-09 | Poison row stalls the queue (F-01) | **Fixed** | Undecryptable rows deferred, content kept; `deferred dispatch` alert; `tests/integration/test_remediation.py` |
| OI-10 | Worker exits on a transient storage error | **Fixed** | Worker recovery loop; tests |
| OI-11 | Lost late-success evidence (F-05, F-10) | **Fixed** | Success fenced to the exact attempt; claims released on rate limit (19 Nov); provider call bounded by the lease |
| OI-12 | OTP waits behind a claimed batch (F-06) | **Fixed** | `batch_size = 1` default; OTP-first claim pass (SMS-03); one 8028 slot per second kept for OTP (SMS-10) |
| OI-13 | Missing time index (F-02) | **Fixed** | `0002`; `0004` and `0005` add the claim-by-age and `created_at` indexes |
| OI-14 | Redaction misses later loggers | **Fixed, known limit** | Handler-level redaction, uvicorn access-log fix with test. Direct stream writes and third-party exporters are not covered |
| OI-15 | Worker leases a channel it cannot send | **Fixed** | `claim_due` filters by the worker's channels |
| OI-19 | Telegram bot key without a worker token | **Fixed** | Worker refuses to start; no Telegram caller in Phase 1 |
| OI-30 | DA visit expiry beyond 7 days (F-07) | **Fixed** | `examples/da_visits.py` caps below the horizon; `test_da_visit_beyond_the_horizon_expires_inside_it_and_is_accepted` (20 Nov) |
| OI-31 | Endless status polling (F-08) | **Fixed** | `poll_complete`, `needs_attention`; polling stops after `receipt_wait_days` |
| OI-62 | Missing process entry points | **Fixed** | One image: `api`, `worker`, `receipts`, `cleanup`, `stats-refresh`; kind install |
| OI-63 | No database grant procedure (F-09) | **Fixed** | `deploy/sql/grants.sql` (now including the `stats_*` tables) |
| OI-64 | No observability (F-11) | **Fixed (20 Nov)** | Heartbeat, `/livez` `/readyz`, `status --fail-on-alert` CronJob; **statistics API** `GET /api/v1/charts` (dashboard contract, read-only reader token), `tests/integration/test_int_charts.py`. Reports Central needs a static-token option to read it (docs/integrations/reports-central.md) |
| OI-72 | Coordinated stop for rollout | **Fixed (20 Nov)** | [deployment/rollout.md](../deployment/rollout.md): expand-only migrations, order, flags after the roll, rollback |

## Changes since 15 November

| Item | Where |
|---|---|
| SMS of up to 3 parts; OTP one part (SMS-09) | PR #5 |
| Broadcast ageing; broadcasts can be switched on (SMS-03) | PR #7 |
| 8028 answers: 4 SMS/s pacing (SMS-10), 500 retried (SMS-11), receipts `status` only (SMS-07), callback IP | PR #8 |
| Statistics API (SMS-12), rollout plan, OI-30 test | PR #12 |

## Go-live checklist (not code)

| # | Item | Owner | State |
|---|---|---|---|
| G-1 | Independent code review of `main` passes | Reviewer | Open |
| G-2 | Dev and Staging environments: hostnames, private CA, fixed egress IP (OPS-02) | DevOps | Open |
| G-3 | Our receipt URL per environment sent to 8028; 213.55.73.209 allow-listed | DevOps | Open |
| G-4 | 8028 credentials: new client secret, strong callback secret (current one is temporary) | 8028, delivery team | Open |
| G-5 | 8028 callback CA reissued with Key Usage (needed before any Python 3.13 image) | 8028 | Open |
| G-6 | Controlled live test on the real 8028: mutual TLS, a real send, a real callback, `status` values | Delivery team, DevOps | Open |
| G-7 | Load test at declared volume with 4 SMS/s pacing; security review; Amharic text review; ATI UAT (spec §8.1 step 3) | QA, security, ATI | Open |
| G-8 | Statistics reader token issued to Reports Central; Reports Central static-token option | OAN operator, Reports Central team | Open |
| G-9 | `OAN_MSG_BROADCAST_ENABLED` on after the 3.1.0+ roll, if DA broadcasts go live | DevOps | Open |

## Known limits (accepted unless a reviewer objects)

* Redaction does not cover direct stream writes or third-party exporters (OI-14).
* No idempotency at 8028: an ambiguous submission (timeout after sending, any 5xx but 500)
  stays `unknown` and is never resent.
* Batching identical texts (SMS-05) is not built; at 4 SMS/s it changes little.
* Latency percentiles in the statistics API are bucket upper edges, not exact values.
