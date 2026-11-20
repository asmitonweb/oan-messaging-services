# Appendix A: v1.0 custom router (fallback)

RP-D1, RP-D2, RP-D4 and RP-D6 were accepted on 19 November 2026 (§8.2). If RapidPro is
later rejected, the bot is built as a custom service
instead of on RapidPro. Every rule in chapters 5.4 to 5.7 and chapter 6 still applies.
Only the platform changes.

| Area | Custom router (v1.0) |
|---|---|
| Stack | Python 3.12, async, python-telegram-bot v21 or later; Redis for state and queues; PostgreSQL for questions; one image and Helm chart `oan-telegram-bot` |
| Receiving | Long-poll `getUpdates`; exactly one receiver per bot token; no public endpoint. Accept `message`, `callback_query` and `contact` updates; deduplicate `update_id` for 24 hours. |
| Buttons | Inline buttons (`callback_query`) |
| State | Redis keys: `tg:c:{contact_ref}` (chat id, language, kebele, active; 90 days), `tg:s:{contact_ref}` (step; 30 minutes), `tg:u:{update_id}` (24 hours), `tg:r:{kebele}:{specialism}` (round-robin pointer), `tg:in` and `tg:q:*` (queues) |
| Outbound | Two lanes (replies first); about 1 a second per chat and 25 a second overall; pause on 429; a sent marker prevents double sends |
| Run modes | `combined` (1 pod) in Development; `split` (1 receiver, 2 or more handlers) before the load test. Receiver and combined Deployments use `Recreate` with 1 replica; handlers roll with `maxUnavailable: 0` and a PodDisruptionBudget. |
| Forwarding | Telegram forwards in both directions: the Development Agent sees the farmer's Telegram name |
| Reply matching | `reply_to_message_id` |
| Egress | `api.telegram.org:443` (by hostname on Istio), Master Data, Marketplace, DA backend, beneficiary API, Auth, Keycloak, Audit Manager, Redis, PostgreSQL |

The farmer linking (MOD-08), kebele routing (§6.4, including the private-chat inbox), the
separate SMS channel and Telegram-only Development Agent alerts are the same in both
options. With the custom router, inline buttons may replace the reply keyboards.
