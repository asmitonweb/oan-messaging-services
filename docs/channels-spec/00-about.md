# 0 About this document

## 0.1 What it is

This is the single specification for how OAN reaches farmers and staff through
**SMS** and **Telegram**.

It also summarises the OAN Messaging Service design (`docs/architecture.md`, contract
3.1.0) and the Frappe connector, so the SMS channel is specified in the same place.

**Not in this document:** step-by-step integration for each module team. That is the
*OAN Messaging integration handoff* pack, which follows the rules here.

## 0.2 Status

| Item | Status |
|---|---|
| Document | **Review.** Version 2.0, 16 November 2026; Telegram chapters 5, 6 and 8 revised 19 November 2026 (RapidPro decisions, area broadcasts, availability, weekly report), then again the same day: broadcasts follow the DA web app and reach linked farmers, welcome SMS, DA backend roster, closing and Supervisor rules, and SMS changes in chapters 3, 4 and 7 (broadcast ageing, batching, receipt formats, purposes). Not yet reviewed by ATI, security or the module teams. |
| SMS: OAN Messaging Service | Built and tested: the full suite (896 tests) passes on Python 3.13 after the 15 November test-certificate fix, and passed on Python 3.12 before it (3.11 and 3.12 not yet re-run after the fix). In independent review; not deployed. 12 of 16 release blockers fixed pending review (§7.5). |
| SMS: Frappe connector | Trial build; tested end to end with Credit (A2C) in a local trial stack. Not released. |
| Telegram bot | Specified only. Platform: RapidPro with OAN Bot Services; RP-D1, RP-D2, RP-D4 and RP-D6 decided on 19 November 2026 (§8.2). Delivered in one phase, built in two waves (§8.1). |

## 0.3 Who reads what

| Reader | Read |
|---|---|
| Everyone | Chapters 1 and 2 |
| Module teams that send SMS (Credit, DPI, Grievance, DA, Auth, eSignet) | Chapter 4, then the integration handoff pack |
| OAN Messaging developers and reviewers | Chapters 3 and 7 |
| Telegram bot developers (RapidPro flows and OAN Bot Services) | Chapters 5 and 6 |
| DevOps and security | Chapters 3.6, 5.3 and 7 |
| ATI and product owners | Chapters 1, 2 and 8 |

## 0.4 Conventions

* Hosts are `example.invalid` placeholders, except two real, documented hosts: 8028's
  published API host (in the 8028 contract notes) and the Master Data development host
  (§5.6). Phone numbers are `0900000xxx` test numbers. No real token or number appears.
* "Registered phone" means the farmer's phone number held in the farmer registry and
  returned by the beneficiary API.
* Words in `code` are field, table or command names.
* **Open** marks something not yet decided; §8.3 lists every open decision.
