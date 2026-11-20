# RapidPro → OAN Messaging Service: proposed integration contract

**Status: FUTURE SCOPE, not planned for Phase 1. Nothing in this document is implemented.**
In Phase 1 RapidPro runs the Telegram bot itself and sends no SMS through OAN (OAN
Channels Specification v2.0). No RapidPro connector,
bridge, callback or status-push endpoint exists in OAN today. Missing pieces are listed
as future work (§8).

## 1. Sources verified

All behaviour below was read from RapidPro's courier source at **nyaruka/courier tag
`v26.3.82`, commit `4e97b1ceede9bbdd297b663d4113ead3b2b2513b`** (committed
2026-11-12; the latest tag at the time of review).

| File | SHA-256 of the copy read |
|---|---|
| `handlers/external/handler.go` | `f90fa1857aed08b3da197999dbb3f17aeb816c4865c488b13bf4ccff29777778` |
| `handlers/split.go` | `80190855c9e6a640e8d577589d40cea4de9b1902041873d7782d4a5de6863371` |
| `handlers/errors.go` | `f40e2383dcd4f6b1009fc6ce9bae38ae8c9745539fba5395f3c320535b1eafd3` |
| `core/channels/errors.go` | `6e6b6cd480ee7fe791397d00cefa087997de6563c0dbf532f25e7133c164cff7` |
| `web/server.go` | `f95b86af5756c2bf5f7647fffd95ac3202f882ea87e88fa4c336871c6a9935ab` |

Behaviour of mailroom and the RapidPro web app (message retry scheduling, UI settings)
was **not** verified here. Those statements are marked *unverified*.

## 2. What courier's External ("EX") channel actually does (v26.3.82)

* **Outgoing send** (`Send`). For each message part, courier makes one HTTP request to
  the channel's `send_url`, using `send_method` (default `POST`), `content_type`
  (`urlencoded` default, `json`, `xml`) and a `send_body` template. The variables are
  `{{id}}` (deprecated) and `{{uuid}}` (the RapidPro message UUID), `{{text}}`,
  `{{to}}`, `{{to_no_plus}}`, `{{from}}`, `{{from_no_plus}}`, `{{channel}}`,
  `{{session_status}}` and `{{quick_replies}}`. Variables are substituted into the URL
  and body **only**.
* **Headers are not templated.** `send_authorization` and `send_headers` are copied
  literally (`req.Header.Set(hKey, fmt.Sprint(hValue))`), so courier cannot put a
  per-message `Idempotency-Key` header on the request.
* **Splitting.** `SplitMsgByChannel` → `SplitText(text, max_length)` (default 160) splits
  long text into several parts, each sent as a separate request **with the same
  `{{uuid}}`**. `SplitText` measures **UTF-8 bytes** (`len(text)`, `bytes.Buffer.Len()`),
  not characters or SMS units. Each Ethiopic character is 3 bytes, so with
  `max_length=160` an Amharic message is split after about 53 characters.
* **Response handling** (`handlers.ErrorFromResponse`):
  * transport error or 5xx → `ErrConnectionFailed` (retryable)
  * 429 → `ErrConnectionThrottled` (retryable)
  * other non-2xx → `ErrResponseStatus` (not retryable)
  * `mt_response_check` optionally requires a substring in the response body

  How retryable errors are re-queued is decided outside courier (mailroom) and is
  *unverified* here.
* **Status callbacks** (receive routes). `GET|POST /c/ex/<channel-uuid>/sent`,
  `/delivered` and `/failed`, with form/query parameter `uuid` (or deprecated `id`)
  holding the RapidPro message UUID. These are the only statuses the External channel
  maps: sent, delivered, failed. `/c/ex/<channel-uuid>/receive` handles inbound messages
  and `/stopped` handles stop events. Both are out of scope.

## 3. Consequence: a small bridge is required (future work)

Pointing courier's External channel straight at OAN **cannot work**. OAN requires an
`Idempotency-Key` header per delivery (contract 3.0.0) and rejects unknown body fields,
and courier cannot template headers. The proposal is therefore an **OAN–RapidPro bridge**
(not implemented) that:

1. receives courier's External-channel request (JSON body template, §4);
2. calls OAN with `Idempotency-Key = rapidpro:<uuid>` and the mapped body, using its
   own OAN credential (`oan_client` can be reused);
3. answers courier **2xx only after OAN returned `202` or `200`**, `5xx` on an uncertain
   OAN outcome, and `4xx` on a definite rejection;
4. stores `(rapidpro uuid → OAN message id)` and pushes status changes back to courier's
   `/c/ex/<channel-uuid>/{sent|delivered|failed}?uuid=<uuid>` (§6).

The pure request mapping (key and reference derivation, recipient check, refusal of OTP
and of over-long unsplit text) is sketched and tested in `examples/rapidpro_mapping.py`.
It cannot detect fragments that courier has already split (§5).
It is **not** a connector: there is no HTTP server, credential store or status poller.

## 4. Proposed External-channel configuration

```text
send_url        = https://<bridge-host-TBD>/rapidpro/send      # placeholder
send_method     = POST
content_type    = json
send_body       = {"uuid": {{uuid}}, "to": {{to}}, "text": {{text}}, "channel": {{channel}}}
send_authorization = Bearer <bridge credential for this RapidPro workspace>
max_length      = 640    # see §5: protects valid pilot text; does NOT stop splitting of longer input
mt_response_check = "accepted"   # optional: require the bridge's success marker
```

With `content_type=json`, courier JSON-encodes each variable, so the template above
yields valid JSON.

## 5. Mapping

| RapidPro / courier | OAN request | Notes |
|---|---|---|
| `{{uuid}}` (message UUID) | `Idempotency-Key: rapidpro:<uuid>` | Stable per message, so courier's retries become same-key replays (`200`, same id) and never a second SMS |
| `{{uuid}}` | `source_reference: rapidpro:<uuid>` | Correlation; the bridge also stores uuid → OAN id |
| `{{to}}` (E.164 from the URN) | `recipient` | OAN accepts `+2519…` / `+2517…` only |
| `{{text}}` | `text` | Must fit **one SMS** (160 GSM-7 or 70 if any Amharic), else `422` |
| bridge config per channel | `purpose` | One channel per purpose (e.g. `broadcast` for campaigns, `transactional` for flows). Never `otp`: OTPs come from eSignet directly |
| (none) | `expires_at` | Optional per channel policy; bounded by OAN ceilings |

**Splitting hazard (unsolved; a prerequisite for any bridge).** OAN's pilot accepts
exactly one part. Courier splits *before* the bridge sees the message, by UTF-8 bytes, and
sends every fragment with the **same `{{uuid}}`**. Consequences:

* A first fragment that fits one SMS is indistinguishable from a complete message. The
  bridge would map it and OAN would accept and send it: the recipient gets a **truncated**
  message. Any later fragment carries the same key with different text and gets `409`.
* Neither the UUID nor the text lets the bridge reconstruct the original message or
  reliably detect that a request is a fragment. Heuristics (length close to `max_length`,
  a second request for the same UUID) arrive too late or are unreliable.
* `max_length = 640` keeps courier from splitting any **valid** pilot message (160 GSM-7
  characters, or 70 with Amharic: at most 320 UTF-8 bytes), so those reach the bridge
  whole. It does **not** disable splitting: text longer than 640 bytes is still split.

Before a bridge can be built, one of these must hold and be verified against the deployed
RapidPro/mailroom version (neither is verified or implemented in MSG-06):

1. **Validation before courier splits.** The flow, campaign and API paths that feed this
   channel refuse text that does not fit one SMS (160 GSM-7, or 70 if any Amharic), so
   courier never receives over-long text. Authoring guidance alone is not enough.
2. **A demonstrably non-splitting configuration with a hard input bound.** RapidPro (or
   mailroom) enforces a hard maximum of *N* characters on outgoing text for this channel,
   and `max_length ≥ 4 × N` bytes (UTF-8 uses at most 4 bytes per character), so no
   accepted input can be split. For example, *N* = 160 needs `max_length ≥ 640`. Over-long
   but unsplit text is then refused by the bridge (`examples/rapidpro_mapping.py`
   `fits_one_sms`) and by OAN with `422`, a visible non-retryable failure (bridge →
   courier `4xx`).

`examples/rapidpro_mapping.py` refuses over-long *unsplit* text and provides
`is_non_splitting(max_length, N)` for checking option 2. It cannot detect fragments.

## 6. Status mapping (bridge → courier)

| OAN status | Courier callback | Notes |
|---|---|---|
| `accepted`, `sending` | none | RapidPro already shows the message as sent to the channel after the 2xx |
| `sent` | `/sent?uuid=` | Provider accepted it; not handset delivery |
| `delivered` | `/delivered?uuid=` | 8028 receipt |
| `failed`, `expired` | `/failed?uuid=` | Definite non-delivery |
| `unknown` | **none** | The External channel has no "unknown" status. Reporting `failed` would invite a manual resend of a message that may have been delivered. The bridge must surface `unknown` to operators separately |

Status discovery: OAN has no push to callers, so the bridge would poll
`GET /api/v1/messages/{id}`. A push mechanism would be future OAN work.

## 7. Ownership and duplicate avoidance

* **Retry ownership.** Courier (and RapidPro) own retries of the bridge call; the bridge
  owns bounded same-key retries to OAN; OAN alone owns gateway submission and never
  resends an ambiguous one. Because the key is derived from the message UUID, every
  retry at every layer is a replay, not a new SMS.
* **No second sender.** A message must reach OAN through exactly one path. Grievance,
  eSignet and DA notifications go directly from their services and must not also be sent
  by RapidPro flows.
* **Broadcast audience authorization stays in RapidPro / the originating application.**
  OAN checks only that the bridge's caller may use the purpose.
* **Priority stays with the OAN worker.** RapidPro traffic uses `broadcast` or
  `transactional` and is always dispatched after OTP traffic.
* **Rate limits.** OAN applies provider limits after acceptance, so the bridge gets
  fast `202`s. Courier's own throttling is unaffected.

## 8. Future work (not in MSG-06)

1. The bridge service itself (§3), with its own caller credential, uuid → id store and
   status poller. **Prerequisite:** §5 option 1 or 2 verified on the deployed RapidPro
   version; without it the bridge can send truncated messages.
2. Optional OAN status push (webhook) to replace polling.
3. Verify mailroom retry scheduling and RapidPro UI configuration against the deployed
   RapidPro version.
4. Multipart support, if 8028 confirms limits and the contract is changed.
