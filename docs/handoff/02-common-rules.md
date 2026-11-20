# 02 Common rules (every caller)

The full contract is `code/openapi.yaml` (contract 3.2.0). This page is
what a developer must get right.

## 1. Endpoints

| Call | Use | Success |
|---|---|---|
| `POST /api/v1/messages/sms` | One SMS to one Ethiopian mobile | `202` new; `200` identical replay |
| `POST /api/v1/messages/telegram` | One plain-text message to a known chat | `202`; `200` replay |
| `GET /api/v1/messages/{id}` | Status of **your own** message | `200`; `404` for unknown or other callers' ids |

Headers:
* `Authorization: Bearer <token>` on every call;
* `Content-Type: application/json` on POST;
* `Idempotency-Key` on POST: 8–128 characters of `[A-Za-z0-9._:-]`, starting with a
  letter or digit (`source_reference` follows the same pattern, 1–128 characters).

SMS body (unknown fields are rejected):

```json
{
  "recipient": "0900000201",
  "text": "Grievance G-1042: the response deadline was extended. We will update you by SMS.",
  "source_reference": "grievance:G-1042:deadline_extended",
  "purpose": "transactional"
}
```

## 2. The ten rules

1. **One key per intended message, created once, stored, and reused on every retry.**
   * With an outbox (the Frappe connector, or your own table): a random UUID4 stored on
     the row.
   * Without an outbox (OTP senders that send at once): the client library's
     `delivery_key(service, event_id, role, channel)`, which gives the same UUID5 for
     every retry of one intended message. Use a new `event_id` for every new code.
   * Never build a key from a row name that can restart, or from personal data. Send the **same body** with the same key; a different body under the same
   key is `409`.
2. **A retry is not a resend.** After a timeout, `5xx` or `429`, send the same request
   again with the same key: OAN answers `200` with the original message if it already
   has it. A **resend** (a new message the user asked for) is a **new key**.
3. **Never resubmit an `unknown` message**, not even under a new key. The farmer may
   already have it. Flag it for a person.
4. **`202` means accepted, not sent.** Show "Submitted" or "In progress" in your UI, never
   "Sent", until the status says so.
5. **One SMS part only:**
   * 160 GSM-7 characters;
   * **70 if the text has any Amharic** (Ethiopic) character, curly quote, en dash or
     emoji.

   Measure every template in every language with the longest real values. The connector
   refuses longer text by default. (Since contract 3.1.0 OAN itself accepts up to 3 parts,
   for DA broadcasts only; an OTP must always be one part,
   `errors[].code = too_long_for_single_sms`.) Nothing changes for your module.
6. **Purpose:**
   * `otp`: needs `expires_at`, at most 30 minutes ahead; highest priority.
   * `transactional`: case updates and notices.
   * `alert`: urgent notices.
   * `broadcast`: DA broadcasts only; not for module notices.

   Your caller is allowed only the purposes the operator granted.
7. **Expiry is yours and never extended.** Pass the deadline after which the message is
   useless: the OTP's own expiry, or the visit time. Compute it **once**, store it, and
   reuse it on retries. At most 7 days ahead for non-OTP messages.
8. **`source_reference`** names the business event, for example `a2c:APP-2026-00017:loan_disbursed`.
   It is not unique: one event sent to two people shares it. It must never contain a
   phone number or personal data.
9. **Poll, don't wait.** Use GET with backoff, starting at 1 minute and capped at
   6 hours.
   * Stop at a final status (`delivered`, `failed`, `expired`).
   * For Telegram, stop at `sent`.
   * Stop at `unknown`, and flag it.
   * Stop when the receipt window ends (7 days for SMS).
10. **No secrets or personal data in logs:** never log the token, the message text (it
    may hold a code) or the full phone number.

## 3. What to do with each response

| Response | Meaning | Do |
|---|---|---|
| `202` / `200` | Accepted (or an identical replay) | Store the OAN `id`, mark Submitted, start polling |
| `409 idempotency_conflict` | Same key, different body | Bug in your code: you changed the text or recipient. Mark Failed and alert. |
| `422 validation_failed` | Bad number, too long, expired, OTP without expiry; `errors[].code` says which | Mark Failed and alert. Do not retry unchanged. |
| `401 unauthenticated` / `403 forbidden` | Token wrong, or channel or purpose not permitted (`errors[].code = purpose_not_permitted`) | Configuration problem: alert the operator. Do not retry in a loop. |
| `413` / `415` | Body too large or wrong content type | Bug: fix the code |
| `503`, timeout, connection error (or a `429`/`5xx` from a gateway in front of OAN) | Uncertain | Retry **later**, with back-off, using the **same key and body**. OAN itself sends no `429` or `Retry-After`; honour a gateway's if present. |

## 4. Status meanings for your UI

| OAN status | Show | Final? |
|---|---|---|
| `accepted`, `sending` | In progress | no |
| `sent` | Sent (SMS: waiting for delivery report) | Telegram: yes for display |
| `delivered` | Delivered | yes |
| `failed` | Not delivered (`failure_code` says why) | yes |
| `expired` | Not sent in time | yes |
| `unknown` | Uncertain: being checked | stop polling; a person checks |

## 5. Phone numbers

* **Farmers:** the registered phone from the beneficiary API (the farmer registry). The
  route and its access rules are being set by the identity owner and DPI (guide 22);
  until then, keep using your current field and note it as a cut-over item.
* **Staff and Development Agents:** from your own system.
* Never a number someone typed into Telegram.
* If there is no phone, do not send; record it as a local failure that a person sees.

## 6. Test data

* Only `0900000xxx` numbers and `example.invalid` hosts.
* Never real phone numbers or tokens in code, tests, tickets or logs.
