# 8028 SMS Gateway — integration contract summary

Source: **8028 SMS Gateway API — Integration Guide for Protean, version 1.1 (12 November
2026)**, with its README and Postman collection v1.3 and 8028's client and callback
samples (received 16 November 2026). It replaces the earlier 12-page guide. This is a
working summary, not a copy. Where the material contradicts itself or this service's
delivery rules, the **Decision** column says what this service does. Items marked
**OPEN** need an answer from 8028 before go-live.

Credentials (client certificate and key, client secret, callback secret) are never
written here, in the repository or in logs. They live in the secret store only.

## Transport and authentication

| Item | 8028 guide v1.1 | Decision |
|---|---|---|
| Base URL | `https://8028.et:3400/sms-api/v1` (outbound TCP 3400 must be allowed) | Default `OAN_MSG_SMS8028_BASE_URL`; configurable |
| Send | `POST /sms/send-sms`, `Content-Type: application/json`, UTF-8 | — |
| Our client authentication | Mutual TLS with the client certificate 8028 issued us **and** `clientSecret` in the body | Certificate and key from a Secret; secret from a Secret; never logged |
| Certificate registration | 8028 registers our certificate by CN and SHA-256 fingerprint. A replaced or renewed certificate must be registered first, or every request returns 401 | Renewal is an operator task (runbook); the current certificate expires on 6 November 2027 |
| 8028 server certificate | Publicly trusted; no extra CA; verification must stay on | `OAN_MSG_SMS8028_CA_CERT` left unset; verification never disabled |
| Time zone | EAT (UTC+3), `YYYY-MM-DD HH:mm:ss` | Adapter converts from aware UTC; receipts converted back |

## Send request

```json
{"clientSecret": "<secret>", "smstext": "…", "phonenums": ["09XXXXXXXX"], "pushdatetime": "2026-11-12 06:00:00"}
```

| Field | Guide | Decision |
|---|---|---|
| `phonenums` | At least one; each exactly `09XXXXXXXX` or `07XXXXXXXX`; `+251…`/`251…` rejected; all or nothing | **One recipient per request** (one message = one `sms_id`). The adapter converts E.164 to `09…`/`07…` |
| `pushdatetime` | Optional; omitted or empty sends now; a past **date** is rejected | Sent as "now" in EAT (today's date, so accepted). Scheduling is out of scope |
| `smstext` | Not empty; trimmed; Unicode supported; no length limit (body < 100 KB); multi-part joined on the handset | **Up to 3 parts** (SMS-09, 20 Nov 2026): one part is 160 GSM-7 characters (extension characters count 2), otherwise 70 UTF-16 units; a longer text is sent whole and counted at 153 / 67 per part, so at most 459 / 201. OTPs stay one part |

## Send response → `ProviderResult`

Success is **HTTP 201**: `{"success": true, "message": "SMS records queued successfully.",
"data": [{"sms_id": "4433006", "phone_number": "0900000901"}]}`. Queued, **not
delivered**. `sms_id` is a string and is the only link to later callbacks.

| Observation | Outcome | `failure_code` | Retry? |
|---|---|---|---|
| 2xx (201 per guide), `success: true`, exactly one `data` entry with our number and an `sms_id` | `sent` | — | — |
| 2xx but `success` not true, `data` empty/multiple/mismatched, or not JSON | `unknown` | `malformed_response` | **no** — 8028 may have queued it |
| 400 (validation, bad JSON) | `rejected` | `invalid_recipient` if the message names the number, else `text_rejected` | no |
| 401 (secret wrong or missing, or certificate not registered) | `not_sent`, not retryable | `provider_auth` | no; channel suspended, operator alert |
| 413 / 415 / other 4xx | `rejected` | `text_rejected` / `provider_rejected` | no |
| 500 | `unknown` | `provider_server_error` | no automatic resend (see open item 1) |
| Read timeout after the request was written | `unknown` | `timeout` | no |
| Connect refused / DNS failure (nothing sent) | `not_sent`, retryable | `provider_unavailable` | yes, with back-off |
| TLS handshake failure (no HTTP status: certificate missing or not accepted) | `not_sent`, not retryable | `provider_config` | no; operator alert |

Error `message` text can contain the phone number (for example `phonenums[1]` values), so
it is **never** stored or logged verbatim; only `http_<code>` is kept.

## Status callback (8028 → this service) → `DeliveryReceipt`

8028 calls the registered endpoint **`https://iiss.et/sms-status-receiver/v1/update-status`**
(guide 8), oldest first, in batches about every 5 seconds:

```json
{"clientSecret": "<callback secret>", "sms_id": 4433006, "phone_number": "251900000901",
 "status": "Delivered", "event_time": "2026-11-11 21:33:20"}
```

| Item | Guide v1.1 | Decision (receipt listener) |
|---|---|---|
| Path | `/sms-status-receiver/v1/update-status` on `iiss.et` | Default `OAN_MSG_RECEIPTS_PATH`; `iiss.et` must route (TCP pass-through) to the listener |
| Server certificate | Publicly trusted, matching the hostname | The listener presents the public certificate for `iiss.et` (`OAN_MSG_RECEIPTS_SERVER_CERT`) |
| Client certificate | 8028 presents its own client certificate; 8028 supplied its callback CA (`CN=StatusAPIPrivateCA`) | Trust that CA (`OAN_MSG_RECEIPTS_CLIENT_CA_CERTS`). Optional pins (`OAN_MSG_RECEIPTS_PINNED_CERTS`) narrow it to specific certificates |
| Callback secret | `clientSecret` in every body; check it on every call | Constant-time check against `OAN_MSG_RECEIPTS_CALLBACK_SECRET` (and `_PREVIOUS` during rotation); wrong or missing → 401; never stored or logged |
| Status field | `status` | Read `status` only (8028 confirmed 20 Nov 2026; values `Pending`, `Delivered`, `Rejected`). The Postman collection's `sms_status` is not read |
| `sms_id` | May be a number; compare as text | Numbers converted to text |
| `phone_number` | International `251XXXXXXXXX` | Normalised to E.164; matched with `sms_id` through the keyed fingerprint |
| Answer | HTTP 200 with `{"success": true}`, or updates stop | Validate, **persist the receipt, then** `200 {"success": true}`; storage failure → 503 so 8028 sends it again |
| Ordering | `Pending` may arrive after `Delivered`; never overwrite a final status | Legal transitions only; final statuses never move |

Status values (guide 8.2):

| `status` | Final | Our status |
|---|---|---|
| `Pending` | no | `sent` (also turns an `unknown` message into `sent`: 8028 has it) |
| `Delivered` | yes | `delivered` |
| `Rejected` | yes | `failed` / `provider_rejected` |
| `Sent`, `Expired` (earlier guide) | no / yes | kept: `sent` / `failed` (`provider_delivery_expired`) |
| anything else, including null or empty | no | recorded (`null`/`empty` for missing values) and acknowledged; no change |

## Inconsistencies in 8028's material

1. **Status field name.** The guide (8.1) and 8028's callback sample use `status`; the
   Postman collection v1.3 (folder 3) sends `sms_status`. **Resolved 20 Nov 2026:** 8028
   confirmed `status` with `Pending`, `Delivered`, `Rejected` and will update the collection.
2. **Status values.** The guide lists `Pending`, `Delivered`, `Rejected`; the Postman
   collection also sends `Sent`, `Failed` and `Expired`. **Decision:** all are recorded;
   only documented values change status.
3. **Callback certificate.** The guide says 8028 presents "its own" client certificate and
   supplies a CA; the README tells testers to present our own client certificate to the
   callback host, and 8028's covering note asks to "use the provided CRT certificate for
   both the client and callback services". **Decision:** trust 8028's callback CA.
   **Answered 20 Nov 2026:** the certificates in the callback sample's `ssl` folder
   (open item 8).
4. **8028's callback CA has no Key Usage extension.** Accepted by the current image
   (Python 3.11). Python 3.13 and later verify strictly and may reject it. **OPEN:** ask
   8028 to reissue the CA with Key Usage before any image upgrade.

## Open items for 8028

8028 answered on 20 November 2026 (relayed by the delivery team). Answered items are kept
here with the answer.

1. **500 means nothing was queued.** **Answered:** confirmed ("SMS will not be queued").
   A 500 is now `not_sent`, retryable, with the worker's normal backoff. Other 5xx stay
   `unknown` and are never resent.
2. **Credentials in the package.** 8028's client sample contains the client secret in the
   same package as the private key, which the guide itself says to keep apart. Ask 8028
   to issue a new client secret, and to confirm whether the key needs reissuing.
3. **Callback secret.** **Answered:** 8028 named the value to use; it is held only in the
   receipts Secret (`OAN_MSG_RECEIPTS_CALLBACK_SECRET`), never in files. It is weak and was
   sent by email: **still to do:** ask 8028 for a strong one before production.
4. **Callback source addresses.** **Answered:** `213.55.73.209`. Allow-list it on the
   receipts listener's ingress or network policy (DevOps, per environment).
5. **Production endpoint and certificate**, if different from the test ones. 8028 also
   asks for **our callback URL** for each environment (DevOps, OPS-02).
6. **Rate limit.** **Answered:** no limit on the API; everything is queued at 8028 and the
   operator sends 2–5 SMS a second. The service now paces 8028 at **4 SMS a second**
   (`OAN_MSG_SMS_RATE_PER_SECOND`, one slot kept for OTP: `OAN_MSG_SMS_OTP_RESERVE`), so
   messages wait in OAN, where OTPs go first and broadcasts age (SMS-03), instead of in
   8028's queue. At that pace a 1 million SMS campaign takes about 3 days.
7. **No idempotency at 8028.** There is no caller reference or status lookup, so an
   ambiguous submission (timeout after sending) stays `unknown` and is never resent.
8. **Callback certificate.** **Answered:** the certificates are those in the package's
   callback sample `ssl` folder (`CA.crt`, `third_party_client_ca.pem`); the listener trusts
   that CA through `OAN_MSG_RECEIPTS_CLIENT_CA_CERTS`. The missing Key Usage extension (inconsistency 4) is still to be raised.
