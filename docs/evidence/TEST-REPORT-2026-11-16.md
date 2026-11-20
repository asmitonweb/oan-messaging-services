# Test report: oan_messaging_services and oan_messaging_connector (16 November 2026)

Everything below was run against **fresh clones from GitHub**:

| Repository | Commit tested |
|---|---|
| `oan_messaging_services` | `main` at `77c8082` |
| `oan_messaging_connector` | `develop` at `a86c10d` |

All providers were mocks or the fake 8028 gateway; no live message was sent. Only
`0900000xxx` test numbers were used.

## Result

**Both repositories pass every functional test.** Three documentation and tooling
problems were found in the local-trial helpers (F1 to F3); none affects the service or
the connector.

| Area | Check | Result |
|---|---|---|
| Service | Full test suite, Python **3.11.15** | **896 passed, 1 xfailed** (132 s) |
| Service | Full test suite, Python **3.12.13** | **896 passed, 1 xfailed** (123 s) |
| Service | Full test suite, Python **3.13.13** | **896 passed, 1 xfailed** (124 s) |
| Service | Type check, `mypy -p oan_messaging` | Pass (27 files) |
| Service | Lint, `ruff check .` as the README says | **Fail: 17 issues, all in `trial/` helper scripts** (F1). Service code is clean. |
| Service | `uv lock --check` | Pass |
| Service | `deploy/docker/requirements.txt` against the lock | Pass: identical to a fresh `uv export` |
| Service | Helm `ci/run-regressions.sh` (lint, render, secret scan, kubeconform) | Pass: **2,824 render checks**, 0 failed |
| Service | `docker build .` | Pass: image 87 MB, runs as uid 10001 with a read-only root file system |
| Service | Runtime commands in the image (`worker`, `receipts`, `cleanup`, `status`) | Pass. The API starts with `uvicorn` (the image's default command) and refuses to start without its required settings, as designed. |
| Trial | `trial/README.md`: `init-env.sh`, `docker compose up -d --build`, CA export | Pass: all six services healthy |
| Trial | Smoke test (`tools/trial.py smoke`) | **Pass** with Python 3.12 and `httpx`: accepted, gateway, receipt, `delivered`, no operator alerts. **The documented command fails as written** (F2). |
| Connector | App tests on Frappe **16.56.0**, Python **3.14.10** (`bench run-tests --app oan_messaging_connector`) | **11 passed** |
| Connector + Credit | Credit end to end (`e2e_a2c.py` with patched `oan_a2c` at `25ceee0`), against the repository's OAN | **Pass: 5 new messages, each `delivered` after 1 poll, each sent exactly once** |
| Consent Manager | OTP end to end (`e2e_consent.py`, patched image), against the repository's OAN | **Pass**: OTP SMS at the gateway; approval with the code; wrong code `401`; re-issue sends a second SMS and the stale code gets `401`; unknown phone `422` with no SMS; no OTP code in the logs |
| Logs | OAN API, worker and receipts logs after the trial runs | No tracebacks, no logging errors, no full phone numbers |

The one xfail is the strict broadcast-starvation test; broadcasts are disabled in the pilot.

## Findings

| ID | Severity | Where | Problem | Suggested fix |
|---|---|---|---|---|
| F1 | Low | `trial/stack/**/*.py`; `README.md` "Develop and test" | `ruff check .` (the README's command) fails with 17 issues in the trial helper scripts: unused imports, and mixed tabs and spaces in the Frappe-side scripts. The service code passes. | Exclude `trial/` in the Ruff configuration (the bench scripts follow Frappe's tab style), or fix the scripts |
| F2 | Medium | `trial/README.md`, `trial/stack/README.md` | `python tools/trial.py smoke` fails as written on a standard Mac: there is no `python` command, and the system `python3` is 3.9, while the script needs Python 3.11 or later (`datetime.UTC`) and `httpx`. Neither requirement is stated. | Document "Python 3.11+ with `httpx`", for example `uv run --no-project --python 3.12 --with httpx python tools/trial.py smoke` (the command used here). The same applies to `e2e_consent.py`. |
| F3 | Low | `trial/stack/compose.yaml`, `consent/compose.yaml`, `a2c/compose.yaml` | The compose project names and the network name `oan-trial` are fixed, and the ports are fixed (18000, 18028, 18100, 18200), so a second trial cannot run next to an existing one. | Document it ("stop any earlier trial first"), or make the names and ports configurable |
| F4 | Info | `pyproject.toml` | `[project.scripts] oan-msg` is declared, but the image installs only the dependencies, so `oan-msg` is not on the image's PATH. No document uses it; every document uses `python -m oan_messaging.runtime`. | None needed; optionally remove the entry or install the project in the image |

## How the trial was run

The machine already had an earlier trial stack using the same names and ports (F3). To
test the repository copy cleanly:

* The earlier stack's containers were stopped (not removed). They were restarted
  afterwards.
* The repository stack ran under its own project name (`COMPOSE_PROJECT_NAME`).
* A local `compose.override.yaml` (not in the repository) renamed only the network.
* Everything else followed `trial/README.md` exactly.

Afterwards, the repository stacks and their volumes were removed. The Credit bench got
its original OAN settings and network back; its connector stays at `a86c10d`, which
differs from the previous `71ef221` only in licence metadata.

## Environment

macOS 27.0.1 (arm64); Docker 29.5.3, Compose 5.1.4; PostgreSQL 16.15 (local, for the
suite); uv 0.11.18; Helm v4.3.1, kubeconform v0.8.0; Frappe 16.56.0 with Python 3.14.10
in the Credit bench container.

## Not covered

* NetworkPolicy enforcement and LoadBalancer behaviour (no enforcing cluster); a
  kind install was done earlier, on 15 November, not repeated here.
* A real 8028 gateway, real Telegram, real handsets.
* Grievance, DA, Auth and eSignet integrations (designed, not built).
* The GitHub Actions workflow (not run on GitHub).

## Addendum: fixes after the independent review (16 November 2026)

An independent static review raised eight findings. All eight were fixed and the affected
tests re-run.

| Finding | Fix | Verified by |
|---|---|---|
| Connector: `queue_sms` could raise on settings, database or queue errors | Whole function guarded; an enqueue failure keeps the row for the one-minute sweep | 2 new tests; connector suite **16 passed** on Frappe 16.56.0 / Python 3.14.10 |
| Connector: OTPs accepted, text kept indefinitely | `otp` refused; daily `purge_content` clears text and masks the recipient of finished rows after a retention period (default 7 days, configurable) | 3 new tests (refusal, purge of finished rows only, recent rows kept) |
| Helm guide described the chart before the runtime work | Guide rewritten for the current chart and `values-pilot.yaml`; chart comments, install notes and guard message updated | Helm regressions: **2,824 checks passed** |
| Credit patch: "paid out" SMS on any completion | "Paid out" only when the bank's stage label is "Disbursed"; a neutral `application_completed` notice otherwise (patch `0002`) | Credit SMS tests **7 passed**; Credit end to end: 5 messages, each `delivered` once |
| Consent patch: an Amharic partner name made the OTP text too long | Text measured against one SMS part (same rule as the service, cross-checked on 20,000 random strings); short fallback without the partner name (patch `0003`) | Consent tests **17 passed** (12 new, including Amharic partner names) |
| Credit trial: `bench init` failed without Redis | `--skip-redis-config-generation` and the bench Redis and database settings documented | Matches the working bench's configuration |
| Spec: privacy notice and Stop promised deletion not yet designed | Notice and Stop now state what RapidPro keeps, how Stop deletes it, and that both depend on RP-D1 | Document review |
| Real-looking phone number; blanket "all hosts are placeholders" claim | Number replaced with a test number; claim narrowed to name the two real documented hosts | Text scan |

Service suite after the changes (Python 3.12): **896 passed, 1 xfailed**. Not re-run: the
Consent Manager end-to-end test (needs an image rebuild; covered by the new unit tests).

## Addendum: 8028 guide v1.1 callbacks (16 November 2026)

8028's integration package (guide v1.1, Postman collection v1.3, samples) showed that the
receipt listener would have refused every real callback. Fixed:

| Change | Verified by |
|---|---|
| Status read from `status` (Postman's `sms_status` still accepted); `Pending` maps to `sent` and never overwrites a final status; null or empty status recorded, not refused | New unit tests; `Pending`, `Delivered`, late `Pending` sequence |
| Callback secret checked in constant time (current and previous); missing or wrong → 401; never stored or logged | New unit test, including log and storage scans |
| Answer `200 {"success": true}`; errors `{"success": false, "error": …}` | Unit and TLS tests |
| Path configurable, default `/sms-status-receiver/v1/update-status`; query strings tolerated | Unit test |
| 8028's callback CA trusted (`OAN_MSG_RECEIPTS_CLIENT_CA_CERTS`); pins optional and narrowing | TLS test over loopback: CA-issued certificates accepted, others refused at the handshake |
| Helm: callback CA, callback secret (with rotation key) and path; guards for missing settings | Helm regressions: **2,828 checks passed** |
| Trial fake 8028: `201` on send; `Pending` then `Delivered` callbacks shaped as guide 8.1 | Local trial from the repository: smoke test `delivered`; listener recorded `Pending` (no change) then applied `Delivered`; fake 8028 saw `{"success": true}` both times; no secret, phone number or traceback in logs |

Service suite after the change (Python 3.12): **901 passed, 1 xfailed**. Ruff and strict
mypy clean.

Not done: anything against the real 8028 endpoint (credentials are not used in tests).
The 500-response handling stays `unknown` until 8028 confirms in writing that a 500 means
nothing was queued.

## Addendum: Frappe `send_sms` hook (16 November 2026)

Explicit calls stay the preferred integration. For Grievance and Auth, which already send
through Frappe's built-in SMS sender, the connector gained an opt-in `send_sms` hook
(`oan_messaging_connector.sms_hook.send_sms`). It registers nothing unless a module adds
the line to its own `hooks.py`.

| Check | Result |
|---|---|
| Connector suite (`develop` `f911344`) on Frappe 16.56.0 / Python 3.14.10, including 17 new hook tests | **33 passed** |
| Hook through Frappe's own `_send_sms`, called the way Grievance and Auth call it | Pass |
| Grievance's SMS delivery logic (`_send_sms_row`, develop `4d03412`) without the hook: still fails closed with no gateway | Pass |
| The same logic with the hook: accepted → Sent with a masked SMS Log; OAN refusal or disabled connector → exception (row Failed) | Pass |
| Auth's `_sms_gateway_configured()` (installed `oan_auth_service`) with and without the hook | Pass |
| Unclear outcome retried with the same key, then raised; one key per recipient; OTP purpose and its 30-minute ceiling; no code or phone in Error Log | Pass |
| One send through the trial OAN API from the bench (`bench execute`, test number) | OAN accepted it (`transactional`, `frappe:send_sms:…`); SMS Log written with a masked number and no text |
| Ruff with the COSS configuration (line length 110, tabs) and Auth's Semgrep rules on the new code | Clean; 0 findings |

Not done: the trial's fake 8028 was stopped, so that message was not carried to
`delivered` (the hook's job ends at OAN acceptance). The Grievance repository was not
modified; its delivery logic was exercised in the connector's tests.
