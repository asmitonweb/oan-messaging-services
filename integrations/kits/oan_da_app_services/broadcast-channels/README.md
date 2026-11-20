# DA Services kit: broadcasts on SMS and Telegram

| | |
|---|---|
| **For** | The DA lead developer: validate, then integrate into `oan_da_app_services` and `oan_da_app_ui` |
| **Base** | `oan_da_app_services` `develop` `2ae3d13`; `oan_da_app_ui` `develop` `4e9dd4d` |
| **Backend** | `app/da_broadcast_channels/`, a Frappe app installed next to `da_services`. **`da_services` is not changed** |
| **UI** | `ui/0001-feat-broadcast-send-on-SMS-and-Telegram-All-farmers-.patch` (6 files) |
| **Upstream stand-ins** | `stubs/stub_server.py` (beneficiary API and Bot Services, standard library only) |
| **Read first** | [`docs/VALIDATION-REPORT.md`](docs/VALIDATION-REPORT.md), then [`docs/BENEFICIARY-API-REQUIREMENTS.md`](docs/BENEFICIARY-API-REQUIREMENTS.md) |
| **Spec** | Channels Specification v2.0 §6.8 and §8.4 (ID-09) |

## What it does

The DA app's Broadcast screen gets working **SMS** and **Telegram** boxes. **All farmers**
now belongs to the Communications Officer and the Admin only, as the FSD says.

| Sender | Recipients | Area | Channels |
|---|---|---|---|
| Communications Officer, Admin | All farmers | A whole region, zone or woreda | SMS, Telegram |
| Communications Officer, Admin | All agents | As today (their grant) | In-app, SMS, Telegram |
| Supervisor / Executive (with the DA role) | All agents | Their own woreda (as today) | In-app, SMS, Telegram |
| Development Agent | All agents | Their own kebele (as today) | In-app, SMS, Telegram |

- **In-app only to agents** still goes to `POST /notifications/broadcast`, untouched.
- **Anything with SMS or Telegram** goes to the add-on's `POST /api/v1/da-services/broadcasts`.
  It records the broadcast in `DA Broadcast` (same history and `client_op_id` rule) and its
  delivery in `DA Broadcast Dispatch`, then sends in the background:
  1. **Telegram first:** `POST /broadcasts` to Bot Services, with the `client_op_id` as
     `Idempotency-Key`.
  2. **SMS copy** when Telegram has finished, or after `sms_fallback_wait_minutes` (15),
     whichever is first.
     - **Farmers:** the area's farmers come from the beneficiary API, page by page, minus the
       Farmer IDs Bot Services reached on Telegram. A farmer whose Telegram copy failed or is
       still pending gets the SMS.
     - **Agents:** the agents not on Telegram (all agents when Telegram is not ticked), minus
       the sender, with phones from the DA Registry.
     - Each SMS goes through `oan_messaging_connector.queue_sms`, with purpose by source
       (SMS-06): `Emergency` and `Weather` as `alert`, `Knowledge` and `Informational` as
       `broadcast`. `source_reference` is `da_broadcast:<DA Broadcast>:<Farmer ID or da:DA-ID>`.
     - Each page's SMS rows and the saved cursor commit together, so a restart never sends a
       page twice.
- **Restarts and retries:** a one-minute scheduler job re-enters unfinished broadcasts.
  - A timeout or 5xx from an upstream is recorded and retried.
  - A 4xx refusal marks the broadcast Failed.
  - `expires_at` (optional, at most 7 days) stops the SMS pass.
- **`GET /api/v1/da-services/broadcasts/<client_op_id>`** returns per-channel counts:
  - in-app recipients;
  - Telegram sent, failed and so on;
  - SMS read, queued, skipped as reached on Telegram, no phone, and the OAN status of each
    queued SMS.
- **SMS length:** up to **3 parts** (OAN Messaging contract 3.1.0, SMS-09). One part is 160
  characters, or 70 with any Amharic letter; beyond that 153 / 67 per part. The screen warns
  when the SMS turns multi-part and blocks Send beyond 3 parts. The API refuses it too.

## Install (DA team)

1. **Connector:** `oan_messaging_connector` `develop` at `96fc5d1` or later. That version
   accepts the `broadcast` purpose and lets a caller ask for up to 3 parts (`max_parts`);
   other modules keep one part.
2. **Backend:** copy `app/da_broadcast_channels` into `apps/`, then:
   - `pip install -e apps/da_broadcast_channels`
   - `bench --site <site> install-app da_broadcast_channels`
   - `bench --site <site> migrate`
3. **Settings** (`DA Broadcast Channel Settings`, or site_config
   `da_broadcast_channels.{beneficiary_api|bot_services}.{base_url, token_url, client_id, client_secret, ca_bundle}`):
   - both base URLs;
   - Keycloak client-credentials for each;
   - `sms_fallback_wait_minutes` (15);
   - `page_size` (500).

   Until the real upstreams exist, run `python3 stubs/stub_server.py --fixture <file>` and
   point both base URLs at it.
4. **UI:** `git am ui/0001-*.patch` on `oan_da_app_ui` `develop`.
5. **OAN operator:**
   - give caller `da` the purposes `alert` and `broadcast`;
   - run OAN Messaging 3.1.0 or later.
   `broadcast` SMS need `OAN_MSG_BROADCAST_ENABLED` on (OAN Messaging with SMS-03
   broadcast ageing, migration 0004). While it is off they are refused.
6. **Folding it into `da_services` later:** the app is written in `da_services`' style (same
   router, `handle_api_errors`, services layer and tests). Moving it in is a file move:
   - `rules.py` and `dispatch.py` to `services/`;
   - `api.py` to `api/v1/`;
   - the two DocTypes and the clients alongside.

## Evidence (20 November 2026, DA trial site)

| Check | Result |
|---|---|
| Add-on tests (`bench run-tests --module da_broadcast_channels.tests.test_broadcast_channels`) | **17 passed**: role and area rules, SMS length (a 3-part copy is queued), paging and resume, Telegram first, the wait, agents not on Telegram, sender skipped, replay and conflict, upstream failures, expiry |
| Full DA suite with the add-on installed | 856 integration + 139 unit tests, **0 failures** |
| Connector suite (with the 3-part change) | **44 passed** |
| UI (`npm run typecheck`, `eslint .`, `vitest run`) | Typecheck clean. Lint 0 errors (2 old warnings elsewhere). 429 tests, of which **20 new pass**. The **11 failures in `grievances/evidence.test.ts` also fail on clean `develop`** |
| Lint (ruff, house settings) | Clean |
| End to end (`e2e/e2e_broadcast.py`): DA site, OAN Messaging 3.1.0 over HTTPS, the fake 8028, the stub | **10 of 10 checks passed** (below) |
| Not checked | The screen in a browser (needs the DA login stack); the real beneficiary API and Bot Services (not built yet) |

End to end, a Communications Officer sent a 2-part Weather warning to all farmers of a
woreda on SMS and Telegram. The woreda had 6 farmers: 2 on Telegram (1 of them failing
there) and 1 without a phone.
- Telegram went first: 1 sent, 1 failed.
- The SMS copy read 6 farmers, skipped the 1 reached on Telegram, counted 1 without a phone
  and queued 4 SMS.
- The fake 8028 delivered all 4 (purpose `alert`).
- An Informational SMS went to OAN as `broadcast` and was delivered (broadcasts enabled, SMS-03 ageing).
- A replay of the `client_op_id` returned the original and sent nothing.

## QA cases

| ID | Case | Expected |
|---|---|---|
| BC-01 | DA or Supervisor opens Broadcast | "All farmers" is not offered |
| BC-02 | Communications Officer picks a region only, All farmers, SMS | Sent to the whole region; `scope: region` |
| BC-03 | Message of 161 English characters with SMS ticked | Warning: "2 parts"; Send allowed |
| BC-04 | Amharic message of 202 characters with SMS ticked | Send disabled: "Too long for SMS: 4 parts" |
| BC-05 | All farmers on SMS and Telegram | Telegram first; SMS only to farmers not reached on Telegram |
| BC-06 | Telegram still sending after 15 minutes | SMS copy starts anyway |
| BC-07 | Same `client_op_id` sent twice | "Already sent", one dispatch, no extra SMS |
| BC-08 | Same `client_op_id`, different text | 409 conflict |
| BC-09 | Agents of a kebele (DA sender) on SMS | Every other agent of the kebele gets it; the sender does not |
| BC-10 | In-app only to agents | Exactly as before (`/notifications/broadcast`) |
| BC-11 | Bot Services refuses (4xx) | Broadcast Failed, error code shown, nothing sent |
| BC-12 | Upstream timeout | Retried by the one-minute job; error code recorded |
| BC-13 | Farmer without a phone | Counted as no phone; nothing sent |
| BC-14 | Weather / Emergency vs Knowledge / Informational | OAN purpose `alert` vs `broadcast` |

## Decisions the DA team owns

- V-05: whether a Supervisor without the DA role may broadcast.
- V-06: storing evidence files so Telegram can send them.
- V-07: wiring Knowledge Base "Send to farmers" to the same route.
- V-08: sending gathering SMS through the connector.
- When to fold the add-on into `da_services`.
