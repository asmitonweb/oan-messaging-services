# 1 Purpose, scope and principles

## 1.1 Purpose

OAN uses two channels:

* **SMS** tells a farmer, or a member of staff, that something happened: an OTP, a loan
  decision, a grievance update, a planned visit. It reaches every phone. It is
  **outbound only**: OAN never receives or reads SMS replies.
* **Telegram** gives farmers a conversation: market prices, official announcements and
  a direct line to their Development Agent, through the OAN Telegram bot.

The two channels are **separate**. A module that sends SMS does not go through the bot,
and the bot sends no SMS.

## 1.2 Principles

1. **One SMS sender.** Every SMS goes through the OAN Messaging Service to the 8028
   gateway. No module talks to 8028 or another gateway directly.
2. **SMS goes to the registered phone.** Each module gets the farmer's phone from the
   beneficiary API (staff phones from their own systems) and sends as its own OAN
   caller. A number given in Telegram is never used for SMS.
3. **Honest outcomes.** `202` means accepted, not sent. `sent` is not `delivered`. An
   uncertain outcome is `unknown` and is never resent automatically.
4. **One key per intended message.** Every SMS has a stable idempotency key, so a retry
   can never become a second SMS.
5. **No secrets or personal data in logs, keys or references.** No message text, full
   phone number, OTP or token is ever logged.
6. **The bot is deterministic.** Every farmer input is a button tap, a menu choice or
   media forwarded unchanged. Every answer comes from an OAN service, approved content
   or a Development Agent. There is no AI, speech or image processing at run time.
7. **The bot keeps the minimum.** Language, kebele and the current step, each with an
   expiry, plus the conversation that RapidPro keeps for the configured retention period
   (RP-D1, 90 days by default, §5.4). Every linked farmer has a subscription record (updates on
   unless turned off, §6.8.1). Only a farmer who links their Farmer ID (§6.6) has a link record, held
   in OAN Bot Services, never in RapidPro.
8. **Narrow routes.** The bot reaches OAN services only through one adapter per
   service, with its own service account.
9. **Telegram arrives only through one secured webhook path** (Design 1, §5.3). The only
   other public Telegram route is the GET-only media route that Telegram fetches relayed
   media from (§5.9). Nothing else of the Telegram platform is reachable from the internet.
10. **Amharic and English** for every farmer-facing text. Designations in Title Case
    ("Development Agent", "Crop Subject Matter Specialist"); wording is conciliatory.

## 1.3 Scope

| Channel | Phase 1 | Deferred | Out of scope |
|---|---|---|---|
| **SMS** | Outbound SMS for Credit (A2C), DPI Consent Manager, Grievance, DA Services, Auth (password reset and Telegram linking OTP) and eSignet (login OTP) | Consent notices (granted, denied, revoked, expired, shared); farmer registry and VeriFayda messages; `broadcast` purpose (needs fairness first) | Inbound SMS, SMS conversations, multi-part SMS, scheduled sends, multi-recipient requests, Novu |
| **Telegram** | Core platform (RapidPro and OAN Bot Services); MOD-01 Market prices; MOD-02 Broadcast channels and area broadcasts through the bot (§6.8); MOD-03 Development Agent handover; MOD-04 Pest and disease escalation; MOD-08 Farmer linking | MOD-05 Advisory content library; MOD-06 Grievance status check; MOD-07 Access to Credit status check; Telegram notices from modules to linked farmers | AI or automated pest identification, weather, payments, credit applications, registry edits, lodging grievances in the bot, consent capture, SMS from the bot |

## 1.4 Which channel for what

| Need | Channel | Sender |
|---|---|---|
| OTP for login, password reset, consent, data release or Telegram linking | SMS, purpose `otp` | The service that issues the OTP (eSignet, Auth, Consent Manager) |
| Case or application update (grievance, loan, visit) | SMS, purpose `transactional` | The owning module, through the Frappe connector or its own outbox |
| Price lookup, question to a Development Agent | Telegram bot | RapidPro and OAN Bot Services |
| Announcement to many farmers | Official Telegram channels (no code), or a targeted area broadcast through the bot (§6.8) | DA Communications Officers, Supervisors and Development Agents, from the DA web app's Broadcast screen |
| Alert or reminder to a Development Agent about a farmer question | Telegram only | OAN Bot Services |
