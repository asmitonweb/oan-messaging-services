# OAN Messaging: integration handoff for development teams

Version: 15 November 2026 (revised after a developer read-through). Status: **Review**.
This pack is for the teams that will make their services send **SMS** through the **OAN
Messaging Service**. The design behind it, for SMS and Telegram together, is the **OAN
Channels Specification v2.0**. The Telegram bot is not an OAN Messaging caller and is not
covered here.

**Contract 3.2.0. Nothing in this pack is deployed.** Development and staging URLs are not
defined yet; every host shown is an `example.invalid` placeholder.

**Changes on 20 November 2026 (no code change for Credit, Consent, Grievance, Auth,
eSignet or DA visits):**

* OAN accepts SMS of up to 3 parts, but only for callers that ask for it (DA broadcasts).
  The connector still refuses text longer than one part by default, so your templates and
  tests stay as they are.
* OAN sends to 8028 at 4 SMS a second (8028 sends 2–5 a second). OTPs always go first.
* A 500 from 8028 is now retried by OAN (8028 confirmed nothing was queued).
* New connector version (`develop` `96fc5d1`): adds the option `max_parts`. You can update
  to it; nothing else changes.
* New in contract 3.2.0: a read-only statistics API (`GET /api/v1/charts`) for the OAN
  dashboards and Reports Central. Module callers do not use it.

## 1. Start here

0. Check you have the **developer package** (§5): the contract, the Python client, the
   Frappe connector and your module's patch, if any. Then ask for what you still need
   using the table in §6.
1. Read [01 Architecture](01-architecture.md) (5 minutes): who calls what, and the three
   integration patterns.
2. Read [02 Common rules](02-common-rules.md): the rules every caller must follow (keys,
   expiry, one SMS part, statuses, never resending `unknown`).
3. Open **your module's guide** (table below) and follow its step-by-step plan.
4. Test locally with [20 Testing and acceptance](20-testing-and-acceptance.md) before
   asking for a dev environment.

## 2. Modules

| Module | Stack | Pattern | What it sends | Status today | Guide |
|---|---|---|---|---|---|
| **Credit (A2C)** | Frappe v16 | Frappe connector | Application submitted, stage changes, approval, rejection, disbursal, agent visits | **Kit built and tested** (`code/kits/`); end to end | [10](10-credit-a2c.md) |
| **DPI Consent Manager** | FastAPI | OTP sender | Consent and data-release OTPs; consent notices (later) | **Kit built and tested** (`code/kits/`); end to end | [11](11-dpi-consent.md) |
| **Grievance** | Frappe | Frappe connector | 12 grievance notices (received, updates, closure, ...) | **Kit built and tested** (`code/kits/`) | [12](12-grievance.md) |
| **DA Services** | Frappe | Frappe connector | Visit planned and rescheduled, to the farmer when the agent ticks the box | **Kit built and tested** (`code/kits/`) | [13](13-da-services.md) |
| **Auth** | Frappe | OTP sender | Password-reset OTP | **Kit built and tested** (`code/kits/`); end to end | [14](14-auth.md) |
| **eSignet** | eSignet (Java) | OTP sender | Login OTP for Farmer ID | **Sender built and tested** (`code/kits/esignet/`); eSignet team wires it in | [15](15-esignet.md) |
| Telegram (OAN endpoint) | none in Phase 1 | Direct API (Telegram) | Not used in Phase 1 | **Closed:** the Telegram bot runs on RapidPro and is not an OAN caller | [16](16-telegram.md) |
| Farmer registry API, VeriFayda | no code yet | OTP sender / notices | e-voucher notices; verification OTPs (expected) | Repositories empty | [11 §7](11-dpi-consent.md#7-farmer-registry-api-and-verifayda) |

## 3. The three patterns

| Pattern | For | What you add | Guide |
|---|---|---|---|
| **Frappe connector** | Frappe apps sending notices | Install `oan_messaging_connector`, then call `queue_sms(...)` at each business event | [03](03-frappe-connector.md) |
| **OTP sender** | Any service sending one-time codes | Store only the hash; send the code **now** with purpose `otp` and the code's own expiry; never store or log the text | [04](04-otp-senders.md) |
| **Direct API** | Non-Frappe notice senders; a future Telegram caller | The OAN client (Python) or plain HTTPS against the contract | [02](02-common-rules.md), [16](16-telegram.md) |

## 4. Everything in this pack

| Document | Contents |
|---|---|
| [01-architecture.md](01-architecture.md) | System picture, components, the patterns, trust boundaries |
| [02-common-rules.md](02-common-rules.md) | API essentials, keys, expiry, length, statuses, errors, polling |
| [03-frappe-connector.md](03-frappe-connector.md) | Install, configure, `queue_sms`, outbox lifecycle, operations |
| [04-otp-senders.md](04-otp-senders.md) | The OTP pattern shared by Auth, eSignet and the Consent Manager |
| [10](10-credit-a2c.md) to [16](16-telegram.md) | One guide per module: messages, call sites, step-by-step plan, configuration, tests, decisions |
| [20-testing-and-acceptance.md](20-testing-and-acceptance.md) | Local trial stack, test levels, the acceptance checklist per module |
| [21-environments-and-onboarding.md](21-environments-and-onboarding.md) | Caller registration, tokens, TLS and CA requirements, configuration per environment |
| [22-decisions-and-open-items.md](22-decisions-and-open-items.md) | Decisions each team owns; known gaps and follow-ups |
| [diagrams/](diagrams/) | Every diagram as Mermaid source (`.mmd`), SVG and PNG |

## 5. The developer package

The handoff zip carries `code/`, `trial/` and `CHANNELS-SPEC/` next to this pack, so you can start without
access to anyone's disk:

| Item | Path in the package | Use |
|---|---|---|
| Contract | `code/openapi.yaml` | The API, contract 3.2.0. Its `servers` entry is a local mock (`http://localhost:8000`); real environments are HTTPS only, so set your base URL explicitly. |
| Python client | `code/oan_client/` | Typed client, `delivery_key`, error classes. Vendored inside the connector too. |
| Sample code | `code/examples/` | One reference implementation per module (Credit, Consent Manager, Grievance, DA, Auth, eSignet), a generic outbox, 12 JSON request/response/error bodies. See its README. |
| Local trial | `trial/` | The OAN service source (clean export, in review) with its tests, the trial stack with a fake 8028, and end-to-end tests for Credit and Consent Manager. Start with `trial/README.md`. |
| Frappe connector | `code/oan_messaging_connector.tar.gz` (source) and `.bundle` (git history, branch `develop`) | `bench get-app` from the unpacked folder or the bundle |
| Integration kits | `code/kits/<module>/` | One patch per module (Credit, Consent Manager, Auth, Grievance, DA), and a Java sender for eSignet, each with its README: `git am` onto the base commit it names. Start with `code/kits/README.md`. |
| QA catalogue | `code/06-qa-cases.csv` | The case IDs cited in each guide |
| Channels Specification | `CHANNELS-SPEC/OAN_Channels_Specification_v2.0.docx` | The design: SMS and Telegram together |

The OAN service source (`main` of 20 November 2026, contract 3.2.0) is in `trial/` only so
you can run it locally;
dev and staging will run the accepted version. Use the local trial (guide 20) to test end
to end.

### The same files in the `oan_messaging_services` repository

| Developer package | Repository |
|---|---|
| `code/openapi.yaml` | `docs/openapi.yaml` |
| `code/oan_client/`, `code/examples/` | `examples/oan_client/`, `examples/` (JSON bodies: `docs/integrations/examples/`) |
| `code/kits/` | `integrations/kits/` |
| `code/06-qa-cases.csv` | `docs/handoff/qa-cases.csv` |
| `code/oan_messaging_connector.*` | the separate `oan_messaging_connector` repository |
| `trial/oan_messaging_services/` | the repository root |
| `trial/oan_integration_trials/stack/`, `repos/` | `trial/stack/`, `trial/repos/` |
| `CHANNELS-SPEC/`, `DEV-HANDOFF/`, `EVIDENCE/` | `docs/channels-spec/`, `docs/handoff/`, `docs/evidence/` |

## 6. Who to ask

Names are still to be assigned by ATI (open item in guide 22). Until then, raise requests
with the OAN Messaging project lead, naming the role below.

| You need | Role that provides it |
|---|---|
| A caller registered and a token | OAN operator |
| Dev and staging URLs, the private CA, secrets in your namespace | DevOps |
| The farmer's registered phone (beneficiary API route and access) | Identity owner and DPI |
| A decision on your module's texts or events | Your product owner |
| A question about this pack or the contract | OAN Messaging project lead |
