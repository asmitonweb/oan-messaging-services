# OAN Messaging operations runbook (pilot)

Status: **Review**. MSG-08 draft for the transactional SMS and Telegram pilot. Thresholds are
provisional and are tuned per environment. No step here sends a message.

All commands run the service image with the release's configuration (for example
`kubectl exec deploy/<release>-api -- <command>`, or a one-off pod using the same
ConfigMap and Secrets). Commands print counts, ages and channel names only. They never
print message content, recipients, chat IDs, keys or connection strings. The one
exception is `issue-credential`, which prints the new token once on stdout.

## 1. Components and commands

| Component | Command | Health |
|---|---|---|
| API | `uvicorn --factory oan_messaging.runtime.api:create_app --host 0.0.0.0 --port 8000` | `/livez` (process), `/readyz` (database) |
| Worker | `python -m oan_messaging.runtime worker` (`OAN_MSG_WORKER_CHANNELS=sms[,telegram]`) | liveness: `python -m oan_messaging.runtime check-heartbeat --max-age 90` |
| Receipt ingress | `python -m oan_messaging.runtime receipts` (path `OAN_MSG_RECEIPTS_PATH`, default `/sms-status-receiver/v1/update-status`) | TCP on 8443 |
| Cleanup | `python -m oan_messaging.runtime cleanup` (CronJob) or `cleanup --forever` (Deployment) | exit status |
| Alert check | `python -m oan_messaging.runtime status --fail-on-alert` (CronJob `status-check`) | Job failure = alert |
| Migrations | `alembic -c migrations/alembic.ini upgrade head`, as the **owner** role | exit status |

Pilot constraints:
* Broadcast is off unless `OAN_MSG_BROADCAST_ENABLED=true` (Helm `config.broadcastEnabled`)
  on the API, the worker and the `caller` command; while off, the API refuses it, the worker
  never claims it and `caller` refuses to grant it. When on, **broadcast ageing (SMS-03)**
  applies: a message waiting longer than `OAN_MSG_BROADCAST_MAX_WAIT_SECONDS` (default
  180) is claimed ahead of newer non-OTP traffic, oldest first; OTPs always go first. A
  large campaign therefore delays newer transactional and alert SMS until it has drained.
* Every enabled channel needs at least one worker with that channel configured.
* The worker refuses to start (exit 1) if:
  * **the latency guard fails:** the database round trip measured at start-up cannot fit
    the 200 ms dispatch window (estimate `4 × p95 + 5 ms` must be at most 100 ms);
  * **the key check fails:** a content or fingerprint key version still used by stored
    data is missing from its Secret. The receipt ingress runs the same key check.

  Each refusal logs the measurement, or the missing version names, never key material.

## 2. Initial set-up

1. Create the database, an owner role (for migrations) and a runtime role with
   `LOGIN`. Set their passwords out of band.
2. Run the migrations as the owner.
3. Apply the grants as the owner:

   ```
   psql "$OWNER_URL" -v ON_ERROR_STOP=1 -v runtime_role=oan_msg_runtime \
        -v owner_role=oan_msg_owner -f deploy/sql/grants.sql
   ```

   Re-run it after every migration that adds a table. Each such migration must add its
   table to the grant list.
4. Store the runtime URL, content keys, fingerprint keys, credential pepper, the 8028
   client secret and certificate, and any Telegram bot tokens in the Secrets named by the
   chart values.
5. Install the chart with `values-pilot.yaml` plus the environment values.
6. Enable the scheduled alert check (`statusCheck.enabled`, on in `values-pilot.yaml`),
   and route **failed Jobs** of the `status-check` CronJob to paging.
7. Onboard each calling service:

   ```
   python -m oan_messaging.runtime caller --caller esignet --channels sms --purposes otp
   python -m oan_messaging.runtime issue-credential --caller esignet
   ```

   Hand the printed token (stdout) straight to the caller's secret store. It cannot be
   shown again. Record the `credential_id` logged on stderr; it is not secret.

   **Rotate** a token:
   1. Issue a new token.
   2. The caller switches to it.
   3. Revoke the old one only:
      `revoke-credentials --caller esignet --credential <old id>`.

   `list-credentials --caller esignet` shows ids and dates, never tokens. On a suspected
   compromise, `revoke-credentials --caller esignet --all` revokes every token at once.

## 3. Monitoring

`python -m oan_messaging.runtime status --fail-on-alert` prints one JSON object. It exits
with code 2 when any alert is active. The chart's `status-check` CronJob runs it every
5 minutes, so an active alert shows up as a **failed Job**. Alongside it, watch pod
restarts (worker liveness) and API readiness.

| Alert | Meaning | First action |
|---|---|---|
| `queue_age` | A due accepted message is older than 5 min | §4.1 |
| `stale_sending` | A sending lease expired more than 5 min ago and has not been recovered | Check that cleanup is running (it recovers stale leases) |
| `unknown_outcomes` | At least one message became `unknown` in the last 24 h | §4.3 |
| `late_starts` | The worker missed its dispatch window in the last hour | §4.4 |
| `deferred_dispatch` | Accepted, never attempted and deferred: usually content that cannot be decrypted | §4.5 |
| `receipt_anomalies` | Orphaned, mismatched or late 8028 receipts in the last 24 h | §4.6 |
| `suspended:<channel>` | Provider dispatch is suspended | §4.2 |

**Statistics for the dashboards.** `GET /api/v1/charts` (architecture, Statistics) gives
Reports Central and the OAN dashboards counts, rates, latency, failures, campaigns, the
live queue and provider health. To give a dashboard access, create a read-only reader and
issue its token (it has no channels or purposes, so it cannot send):

```
python -m oan_messaging.runtime caller --caller reports-central --stats-reader
python -m oan_messaging.runtime issue-credential --caller reports-central
```

The snapshot is rebuilt by the cleanup component every `OAN_MSG_STATS_REFRESH_SECONDS`
(900). `meta.as_of` older than about 30 minutes means cleanup is not running.

## 4. Procedures

### 4.1 Queue not draining

* Check `provider_scopes` in the status output. A suspension or cooldown stops dispatch
  for that channel.
* Check the worker pods: running, not restarting, with the right `OAN_MSG_WORKER_CHANNELS`.
* Worker logs show `worker cycle unavailable error_type=...` while the database is
  unreachable. The worker backs off for up to 30 s and recovers by itself.
* OTP messages still expire on their original deadline while the queue is blocked. This
  is intended.

### 4.2 Provider suspended (credential or configuration fault)

A definite 401/403 or configuration rejection suspends the whole channel. This stops it
from failing the entire backlog. Messages stay `accepted` and keep their expiry.

1. Fix the cause: rotated client secret, certificate, or bot token.
2. Roll the worker so it picks up the new Secret.
3. `python -m oan_messaging.runtime resume --channel sms` (or `telegram`).

To stop a channel deliberately (for example during a provider incident), run
`python -m oan_messaging.runtime suspend --channel sms`. Repeated ambiguous outcomes
(three in a row) cause a 60-second cooldown, which resumes by itself.

### 4.3 Unknown outcomes

`unknown` means the provider may have received the message. OAN **never resends** it, and
callers must not resubmit it under a new key.

1. Reconcile with 8028 or the Telegram logs using the message id and the time.
2. Record the outcome in the owning service's support process.
3. A rising count usually points to a provider incident. See the cooldown in §4.2.

### 4.4 Late starts

The worker reserved a rate slot but could not call the provider within the 200 ms
dispatch window. The message is retried later and nothing was sent.
* Occasional late starts come from short pauses (CPU throttling, node pressure).
* Persistent late starts mean the database round trip is too high. Check the start-up
  `dispatch latency` log line and the worker's CPU limits.

### 4.5 Deferred dispatch (content cannot be decrypted)

The worker defers the row for 5 minutes at a time and keeps the content. It never fails
or purges it.

A worker that is missing a key version in use refuses to start: the log names the
version. Deferral therefore normally means a single corrupt row, or a key that was
removed while the worker was running.

1. Check that `OAN_MSG_CONTENT_KEYS` contains every key version still in use, and that
   `OAN_MSG_CONTENT_KEY_CURRENT` names one of them.
2. Fix the Secret and roll the worker. The rows dispatch on their next due time, unless
   they expired in the meantime.

### 4.6 Receipt anomalies

* `orphaned`: a receipt arrived before the send response was recorded. It is normally
  applied shortly after.
* `mismatched`: the sms_id is known but the recipient differs. Treat this as a security
  event and keep the receipt rows.
* `unmatched_late`: the receipt arrived after the receipt window. It is recorded but not
  applied.

### 4.7 8028 credentials and callbacks (8028 guide v1.1)

| Material | Where | Rotation |
|---|---|---|
| Our client certificate and key (`CN=OTPSMSClient`, expires 6 November 2027) | `secrets.sms8028.tlsSecret` | 8028 registers it by CN and SHA-256 fingerprint: send them the new certificate first, wait for confirmation, then update the Secret in one step and restart the workers. Otherwise every send returns 401 and the channel is suspended. |
| Client secret | `secrets.sms8028.existingSecret` | Update the Secret, restart the workers, run `resume --channel sms` if it was suspended |
| 8028's callback CA | `secrets.receipts.clientCaSecret` (`clientCaKeys`) | Add the new CA as a second key, roll out, remove the old one after 8028 switches |
| Callback secret | `secrets.receipts.callbackSecret` | Store the new value under a new key and point `key` at it; set `previousKey` to the old key; roll out; ask 8028 to switch; then clear `previousKey` and roll out again |
| Our callback server certificate (public, for the callback host) | `secrets.receipts.serverTlsSecret` | Renew before expiry; 8028 rejects a certificate that is not publicly trusted or does not match the host |

Callbacks that fail authentication answer `403` (certificate) or `401` (secret) and are
logged without the secret. A run of them usually means a rotated secret or certificate on
8028's side: check with 8028 before changing anything here.

## 5. Releases and rollback

The version-by-version plan is in [deployment/rollout.md](deployment/rollout.md).


* Stop all workers of the old release before activating schema features that the old
  release cannot read.
* Migrations `0002` (index) and `0003` (`dispatch_controls`) are additive.
* Rolling back the image after `0003` is safe as far as the schema goes. Old images ignore
  the new table and therefore ignore suspensions. Suspend dispatch by scaling the workers
  to zero instead.
* Do not downgrade migrations on a database that holds production data.
