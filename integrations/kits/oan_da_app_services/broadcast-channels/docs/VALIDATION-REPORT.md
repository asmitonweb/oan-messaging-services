# DA broadcast: validation against the Channels Specification

**Checked:** 20 November 2026
**Code:** `oan_da_app_services` `develop` `2ae3d13` and `oan_da_app_ui` `develop` `4e9dd4d`
(no newer commits on any branch at the time)
**Against:** Channels Specification v2.0 §6.8 (area broadcasts) and §8.4 (ID-03, ID-09), and
the DA team's own notes (`docs/api-da-screens.md`, `docs/data-model.md`)

The rule for this package was to fit what is built and change nothing that works, except
real errors or oversights. Each finding says which it is and what the package does about it.

## What is built and works

| Area | Where | State |
|---|---|---|
| In-app broadcast to agents | `services/notifications.py` `broadcast()`, `POST /notifications/broadcast` | Works. Scopes are all, region, zone, woreda or kebele. `client_op_id` is unique at the database, the write is atomic, and each send is audited |
| Who may target what | `_assert_broadcast_scope_allowed` (`services/notifications.py:232`) | Works. The Admin may send anywhere. A Communications Officer stays within their grant (none = national desk). A Supervisor or Executive with the DA role sends to their own woreda. A DA sends to their own kebele |
| Agents of a kebele | `_kebele_recipients` | Works. An Effective `DA Kebele Assignment` plus an active Development Agent grant |
| Broadcast screen | `BroadcastWorkspace.tsx` | Composes, picks the area, and sends in-app to agents |

The package reuses all of these unchanged.

## Findings

| # | Finding | Where | Kind | In this package |
|---|---|---|---|---|
| V-01 | **"All farmers" is offered to every role that can send** (DA, Supervisor, Communications Officer, Admin). The DA team's notes say farmer broadcasts belong to the Communications Officer ("FSD gives broadcast to the Communications Officer", `docs/api-da-screens.md:592`) | `BroadcastWorkspace.tsx:153` | Oversight | UI patch: offered to the Communications Officer and the Admin only |
| V-02 | **The SMS segment counter divides by 160 for every text.** One Amharic letter makes a part 70 characters, so Amharic messages show too few segments | `BroadcastWorkspace.tsx:159`, `SmsPreview.tsx:3`, `SendToFarmersModal.tsx:54` | Error | UI patch: counts as OAN Messaging does (70/67 with Amharic, 160/153 otherwise). Warns when the SMS turns multi-part; blocks beyond 3 parts |
| V-03 | **The screen always requires a woreda.** The backend accepts region and zone, and farmer broadcasts may cover a whole region or zone | `BroadcastWorkspace.tsx` (Woreda field `required`) | Gap | UI patch: for All farmers, Zone and Woreda are optional, and the deepest level chosen is the area. Broadcasts to agents are unchanged |
| V-04 | **SMS and Telegram boxes were disabled** ("not wired yet") | `BroadcastWorkspace.tsx` channels fieldset | Gap (ID-09) | Add-on app and UI patch wire them |
| V-05 | **A Supervisor without the DA role gets 403.** The screen lets a Supervisor send (`canDispatch`), the scope rule has a Supervisor branch, but the route admits only Communications Officer, Admin and DA | `api/v1/notifications.py:74` | Mismatch | **Not changed.** The add-on route admits the same roles. The DA team decides whether Supervisors need the DA role or the route should admit them |
| V-06 | **Evidence files are collected but never sent or stored** | `BroadcastWorkspace.tsx:176` | Gap | **Not changed.** Spec §6.8 sends them on Telegram as media. Needs storage on the DA side first |
| V-07 | **The Knowledge Base "Send to farmers" dialog is a mock.** It uses fixed woredas and sizes and sends nothing | `SendToFarmersModal.tsx` (`BROADCAST_WOREDAS`, `audienceSize`) | Gap (FR-11) | Only its segment counter is fixed (V-02). Wiring it to `POST /broadcasts` is a follow-up |
| V-08 | **Gathering SMS is not sent.** The after-commit job is an empty placeholder, so rows stay Queued | `services/gatherings.py:103` | Gap | **Not changed.** It can use `queue_sms` as the visit kit does |
| V-09 | **No farmer list by area code with phones.** `DA Farmer Reference` holds kebele *names*. The registry client filters by names, and Dev ignored the filter | `integrations/farmer_registry/http.py:553` | Gap (ID-01/04) | The add-on reads the proposed beneficiary API ([`BENEFICIARY-API-REQUIREMENTS.md`](BENEFICIARY-API-REQUIREMENTS.md)); a stub serves it until it is live |
| V-10 | **The DA backend parts of ID-03 are still missing:** Telegram link (D4), routing mode (D5), routes R1–R4 | none | Gap (ID-03) | Not in this package. Telegram staff features wait on them; Bot Services returns agents not on Telegram meanwhile |
| V-11 | **`DA Broadcast` records neither the recipient group nor the channels** | `doctype/da_broadcast` | Design | **Not changed.** The add-on keeps them in its own `DA Broadcast Dispatch`, linked one-to-one |
| V-12 | **Area code formats differ:** `DA Region/Zone/Woreda/Kebele` use bare P-codes (`ET04080101`), `DA Administrative Area` examples use `ET04-W01-K03`, and the new Dev catalogue uses `kebele-ET04…` | doctype descriptions; `oan-helm` commons-services values | Risk | The add-on checks the area code against `DA Region/Zone/Woreda/Kebele`. One format must be settled with the registry (B-1) |

## Dependencies outside the DA team

| Item | Owner | Effect until done |
|---|---|---|
| Beneficiary API routes (ID-01, ID-04) | Farmer registry / DPI | Farmer SMS and the Telegram linking work only against the stub |
| Bot Services `/broadcasts` | OAN messaging (Telegram, wave 1) | Telegram copies work only against the stub |
| `OAN_MSG_BROADCAST_ENABLED` on each environment (SMS-03 ageing is built) | OAN messaging, DevOps | `Knowledge` and `Informational` SMS (purpose `broadcast`) are refused while it is off. `Emergency` and `Weather` (`alert`) are sent now |
| 8028 answers (ID-12) | Delivery team | Multi-part SMS: 8028's guide v1.1 sets no limit; we cap at 3 parts |
