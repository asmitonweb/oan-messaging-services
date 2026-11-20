# Reports Central: OAN Messaging as an upstream

Reports Central (`oan-reports-central`, branch `developer`) reads every OAN service through
the same dashboard contract: `GET {UPSTREAM_<SERVICE>_BASE_URL}/api/v1/charts/{chartKey}`
(`backend/app/services/upstream.py`). OAN Messaging serves that contract (contract 3.2.0),
so adding it is configuration plus a catalogue entry on the Reports Central side.

## What Reports Central needs

| Item | Value |
|---|---|
| Upstream name | `messaging` (`UPSTREAM_MESSAGING_BASE_URL`, the OAN Messaging API base URL) |
| Auth | `Authorization: Bearer <token>` of a statistics reader (OAN runbook §3: `caller --stats-reader`, `issue-credential`). Held as a Secret; never logged |
| Envelope | `{"success": true, "data": [rows], "meta": {"as_of", ...}}`; `unwrap_chart_payload` already handles it |
| Batch | `GET /api/v1/charts?charts=a,b` returns `data: {chartId: {"chartName", "data"}}`, the "gateway style" that `unwrap_chart_payload` handles |
| Filters | `from`, `to`, `granularity`, `caller`, `purpose`, `channel`; region filters are ignored (OAN has no geography) |

## Proposed reports (category "Messaging")

| Report | Charts |
|---|---|
| Messaging summary | `msgKpis`, `msgTrend` |
| Messages by module | `msgByCaller`, `msgFilterCallers` (filter list) |
| Messages by purpose | `msgByPurpose` |
| Why messages fail | `msgFailureReasons` |
| OTP speed | `msgOtpLatency` |
| Broadcast campaigns | `msgCampaigns` |
| Queue and gateway now | `msgQueueNow`, `msgProviderHealth` |

Reports Central is a COSS repository: these changes are for its team (no push from here).

**Auth gap to close on the Reports Central side (checked 20 November 2026, `developer`
`1f9322c`):** `fetch_chart` sends no Authorization header today. The planned upstream auth
(`backend/tests/test_upstream_auth.py`) is a **Keycloak client-credentials** token for every
upstream. OAN Messaging does not accept Keycloak tokens: its statistics reader holds an OAN
token. Reports Central needs a per-upstream **static bearer token** option
(`UPSTREAM_MESSAGING_TOKEN`, from a Secret) used instead of the Keycloak token for this
upstream.
