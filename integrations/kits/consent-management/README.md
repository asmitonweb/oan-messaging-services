# DPI Consent Manager kit: consent OTPs by SMS

| | |
|---|---|
| **Repository** | `consent-management` |
| **Base** | `develop` at `32bce76` (42 commits ahead of `main`, where OTP consent now lives) |
| **Patch** | `0001-feat-otp-deliver-consent-OTPs-by-SMS-through-the-OAN.patch` (12 files) |
| **Handoff guide** | 11 |

## What it changes

| File | Change |
|---|---|
| `services/otp_provider.py` | Both providers send the code through OAN when it is configured, and then report `delivers = True`, the extension point `develop` provides; otherwise the log and bucket harnesses as before |
| `services/otp_delivery.py` (new) | Phone resolution, OTP text that always fits one SMS part, one key per issued code, send through OAN, errors as `OtpError` |
| `services/sms_length.py` (new) | The one-SMS-part rule OAN enforces |
| `config.py` | `oan_*` settings and `oan_delivery_ready` |
| `oan_client/` (new) | The OAN Messaging client, contract 3.0.0, unchanged apart from relative imports |
| `services/otp_service.py`, `services/otp_consent_service.py` | A send whose outcome is uncertain (OAN may still deliver it) still counts against the resend cooldown and the send budgets; a resend then keeps the new code |
| `tests/test_oan_otp_delivery.py` (new) | 14 unit tests |
| `tests/test_otp_sms.py` (new) | 7 integration tests through the partner portal API and the database |

On `POST /consent-requests` with `method: "otp"` and on `resend-otp`, a code that cannot be
delivered refuses the request with the reason: no phone for the subject → 422
`otp_destination_unknown`; OAN refused → 502 `otp_delivery_rejected`; OAN unavailable → 503
`otp_delivery_unavailable`; delivery switched on without a URL or token → 503
`otp_unavailable` (nothing issued, nothing stored).

## Configure

`CONSENT_MANAGER_OAN_DELIVERY_ENABLED=true`, `CONSENT_MANAGER_OAN_BASE_URL` (HTTPS),
`CONSENT_MANAGER_OAN_TOKEN` (caller `dpi-consent`: sms, `otp`), optionally
`_OAN_CA_BUNDLE`, `_OAN_TIMEOUT_SECONDS`, `_OAN_MAX_ATTEMPTS`. Turn `otp_log_codes` and
`otp_publish_enabled` off. `CONSENT_MANAGER_OAN_PHONE_RESOLVER=map` with
`_OAN_OTP_PHONE_MAP` is for dev and staging test numbers only. **Before production:** a
resolver that reads the registered phone from the beneficiary API (plug it in at
`resolve_phone`), approved by the identity owner.

## Evidence (17 November 2026)

| Check | Result |
|---|---|
| Ruff (E, F, W, I, UP, B, RUF, S, BLE; line length 100; Python 3.10) and ruff format on the new files; changes inside existing files follow their style. The repository has no lint configuration of its own | Pass |
| Semgrep: Python correctness and security | 0 findings |
| Full backend suite with the kit / without (PostgreSQL 16, Python 3.12) | 216 passed / 195 passed |
| End to end against OAN over HTTPS and the fake 8028 (the same delivery module on the `main` build, the only image available) | OTP SMS at the gateway; approval with the code; wrong code 401; re-issue sends a new code and the old one gets 401; unknown subject 422 with no SMS; no code in the service log |
