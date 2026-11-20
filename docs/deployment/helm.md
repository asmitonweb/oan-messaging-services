# OAN Messaging Service: Helm chart

| | |
|---|---|
| Chart | `deploy/helm/oan-messaging/` (version `0.1.0-draft.2`, contract 3.0.0) |
| Status | **Draft for review.** It renders and validates, and `ci/run-regressions.sh` repeats every check. With the MSG-08 image and entry points it was installed, recovered and upgraded on a throwaway local kind cluster; it has not been deployed to any shared environment. |
| Baseline | Contract 3.0.0. The service (including the MSG-07b reliability work and MSG-08 runtime) is in independent review. |
| Scope | Standalone Python service on Kubernetes with **external PostgreSQL**. Not a Frappe bench; no MariaDB, Redis or bundled database. |

Nothing in the chart creates a Secret, contains a credential or private certificate, or
points at a real environment. Hostnames, CIDRs and Secret names are placeholders.

## 1. What the code provides today

Every command, variable and path the chart uses was read from the code. A component
whose command is missing refuses to render instead of inventing one.

| Need | In the code | Chart behaviour |
|---|---|---|
| API process | `uvicorn --factory oan_messaging.runtime.api:create_app` (also the image's default command) | Default `api.command`, plus `--host/--port` and `--timeout-graceful-shutdown 20` |
| Schema migration | `migrations/alembic.ini` + `migrations/env.py`, URL from `OAN_MSG_DATABASE_URL` | Default `migration.command: alembic -c migrations/alembic.ini upgrade head` (working directory: `image.workingDir`, MSG-08) |
| Storage settings | `StorageSettings`, prefix `OAN_MSG_` (`storage/config.py`) | `OAN_MSG_DATABASE_URL`, `_CONTENT_KEYS`, `_FINGERPRINT_KEYS`, `_CREDENTIAL_PEPPER` from Secrets; `_CONTENT_KEY_CURRENT`, `_FINGERPRINT_KEY_CURRENT`, `_MAX_BODY_BYTES`, `_DB_POOL_SIZE`, `_DB_MAX_OVERFLOW`, `_POLICY_*_SECONDS` as plain config |
| 8028 adapter settings | `Sms8028Settings.from_environ` (`adapters/sms8028.py:142`) | `OAN_MSG_SMS8028_CLIENT_SECRET` (Secret), `_CLIENT_CERT`, `_CLIENT_KEY`, `_CA_CERT` (file paths), `_BASE_URL` (optional) |
| Telegram tokens | `TelegramBotRegistry.from_environ`, `OAN_MSG_TELEGRAM_BOT_TOKEN__<KEY>` (`adapters/telegram.py:147`) | One env var per entry in `secrets.telegram.bots` |
| Worker process | `python -m oan_messaging.runtime worker`: loads settings and adapters, checks start-up conditions (database latency, key versions, Telegram tokens), stops on SIGTERM, writes a heartbeat each cycle | Off in `values.yaml`; **on in `values-pilot.yaml`** with this command and `worker.channels` |
| Cleanup | `python -m oan_messaging.runtime cleanup` (one pass; `--forever` for the loop) | Off in `values.yaml`; **on in `values-pilot.yaml`** as a CronJob |
| Receipt ingress | `python -m oan_messaging.runtime receipts`: reads the server certificate, 8028's callback CA and/or pinned certificates (mounted at the §3.2 paths), the callback secret and the path | Off in `values.yaml`; **on in `values-pilot.yaml`** with this command |
| Health endpoints | API `/livez` (process serves requests) and `/readyz` (database answers); worker `check-heartbeat` | HTTP probes for the API, an exec probe for the worker (§3.6) |
| Status alerts | `python -m oan_messaging.runtime status --fail-on-alert` (exit 2 on any alert) | `statusCheck` CronJob; off in `values.yaml`, **on in `values-pilot.yaml`** |
| Key-file validator | `build_ssl_context` and `build_server_ssl_context` reject a private key with any group/other bit (`st_mode & 0o077`) | Validated 0600 snapshot of one Secret generation via init container (§3.2) |

## 2. Components

| Component | Kind | Default | Secrets it receives |
|---|---|---|---|
| `api` | Deployment ×2, ClusterIP Service, PDB, optional Ingress | enabled | database runtime URL, storage keys |
| `migration` | Helm `pre-install,pre-upgrade` hook Job + hook ServiceAccount | enabled | **migration database URL only** |
| `worker` | Deployment ×2 | off; on in `values-pilot.yaml` | storage keys, runtime URL, 8028 client secret, 8028 client certificate/key, Telegram tokens |
| `receipts` | Deployment ×2, L4 Service (LoadBalancer by default), PDB | off; on in `values-pilot.yaml` | storage keys, runtime URL, receipt server certificate/key, pinned 8028 certificates |
| `cleanup` | CronJob (default) or Deployment ×1 | off; on in `values-pilot.yaml` | storage keys, runtime URL |
| `statusCheck` | CronJob (`status --fail-on-alert`, every 5 minutes) | off; on in `values-pilot.yaml` | storage keys, runtime URL |

Every component reads the storage keys because `StorageSettings` requires all of them at
startup and the repository needs the cipher and fingerprinter. The 8028 and Telegram
credentials reach the worker only; the receipt server key reaches the receipt ingress only.

## 3. Design decisions

### 3.1 Receipt TLS stays inside the process

The receipt ingress authenticates 8028 by mutual TLS performed in-process (pinned client
certificate, `CERT_REQUIRED`, plus a fingerprint check against the TLS session). The code
has no path that reads a certificate from a header. The chart therefore:

* exposes the receipt ingress only through a **TCP Service** (`receipts.service`,
  LoadBalancer by default, with `externalTrafficPolicy: Local` and
  `loadBalancerSourceRanges`), never through an HTTP Ingress or gateway;
* **fails rendering** if a receipts Service annotation would make the load balancer
  terminate or re-encrypt TLS (`*ssl-cert*`, `*ssl-ports*`, `*ssl-negotiation-policy*`,
  `*backend-protocol*`);
* exposes only `/api/v1/messages` through the optional API Ingress.

Alternatives that keep the peer certificate, if a LoadBalancer is not available:
ingress-nginx `--enable-ssl-passthrough` with the `ssl-passthrough` annotation, or an
APISIX stream (L4) route. Both are operator configuration outside this chart; neither
may terminate TLS.

### 3.2 Private keys: a validated 0600 snapshot of ONE Secret generation

Kubernetes Secret volume files are root-owned. Without `fsGroup` a non-root process
cannot read them; with `fsGroup` they become group-readable (`0440`), which the code's
validator rejects. The worker and receipt pods therefore run an init container
(same non-root uid, no capabilities, read-only root filesystem, `fsGroup` = app gid) that
executes `files/tls_snapshot.py`, embedded verbatim with `.Files.Get`:

1. **Pin one generation.** The kubelet projects a Secret as `..data -> ..<timestamp>/`
   and switches that symlink atomically on update. Opening `tls.crt` and `tls.key`
   separately could cross the switch (old certificate, new key: reproduced in the
   regression fixture). The script resolves `..data` **once**, reads every file through
   that generation directory, and confirms `..data` still points to it afterwards.
2. **Retry the whole set** if the generation switched or vanished during the read (5
   attempts, 1 s apart). After the last attempt it exits non-zero and publishes nothing,
   so the pod does not start. A volume without `..data` is refused (fail closed).
3. **Validate before publishing.** Files are written `0600` (directory `0700`) into a new
   `.gen-<random>` directory in the memory-backed emptyDir, the key is checked for
   group/other bits, `ssl.load_cert_chain` must accept the certificate/key pair, and the
   CA (if any) must load. A mismatched pair is never published.
4. **Publish atomically.** `current -> .gen-<random>` is switched with `os.replace` of a
   symlink. Readers only ever see the previous complete set or the new one. Stale
   generations and temporary links from an interrupted earlier run are removed after a
   successful publish, so an init-container rerun (pod restart) is safe.

The main container mounts only the emptyDir, read-only, and never the Secret that holds a
private key. A main-container restart keeps the published set; a new set is taken only
when the init container runs again (pod restart or rollout).

Mounted paths:

| Pod | Path | Content |
|---|---|---|
| worker | `/run/oan-msg/tls/current/tls.crt`, `tls.key`, `ca.crt` | 8028-issued client certificate and key; optional 8028 server CA. Wired to `OAN_MSG_SMS8028_CLIENT_CERT/_CLIENT_KEY/_CA_CERT` |
| receipts | `/run/oan-msg/tls/current/tls.crt`, `tls.key` | server certificate presented to 8028 |
| receipts | `/etc/oan-msg/receipts/ca/<key>` | 8028's callback CA (public; mounted directly) |
| receipts | `/etc/oan-msg/receipts/pins/<key>` | optional pinned 8028 callback certificates (public; mounted directly) |

Regression coverage (`ci/regression/test_tls_snapshot.py`, 10 tests, run against the
script file itself): the naive mixed-pair hazard, single generation, a switch between
reads (one retry, new generation published), a generation deleted mid-read, continuous
churn (fails, previous set intact, no leftovers), a mismatched pair (never published),
no `..data` (fail closed), restart rerun with crash leftovers, the optional CA, and the CLI
exactly as embedded (exit codes, no key material in output). Kubelet behaviour itself was
not exercised on a cluster.

### 3.3 Migration hook: ordering, networking and upgrade lifecycle

* **What it is.** A `pre-install,pre-upgrade` hook Job for this release: one pod,
  `parallelism: 1`, `completions: 1`, `backoffLimit: 0`, `activeDeadlineSeconds: 600`.
  Alembic runs each migration in its own transaction (`transaction_per_migration=True`).
* **What runs alongside it.** On **install** the Job completes before Helm creates any
  workload. On **upgrade** the previous release's workloads keep running while the hook
  runs, and are replaced only afterwards. Every migration must therefore be backward
  compatible with the running version: expand first (add tables, nullable columns), and
  contract (drop, rename, tighten) only in a later release once no old pod uses it.
* **What serializes it.** Helm's release lock stops two operations on the same release
  from running hooks concurrently. That is **not** a database lock: two releases (for
  example two namespaces) or a manual `alembic upgrade` against the same database can run
  at once. Rule: one release per database, and no manual migrations while a release
  operation is in progress. A database-level guard (for example a PostgreSQL advisory
  lock taken in `migrations/env.py`) is runtime work for MSG-08; this chart does not
  change `migrations/`.
* **Credentials.** The hook has its own ServiceAccount (weight −10) and receives only the
  migration database URL (`database.migration.*`, ideally a separate schema-owner role).
  No storage keys, no ConfigMap, no runtime credentials. The regression checks assert
  this.
* **Networking.** The chart's ordinary NetworkPolicies do not exist yet on first install
  and are the previous release's during pre-upgrade, so they cannot serve the hook.
  `migration.network.mode` makes the path explicit and is validated at render time:

| Mode | Meaning | Render-time rule |
|---|---|---|
| `hook` | The chart creates `<fullname>-migration-egress`, a hook NetworkPolicy (weight −5, before the Job at 0) selecting only the migration pod: no ingress; egress to DNS and `networkPolicy.egress.postgres.cidrs:port`. Recreated with the new values on every upgrade (`before-hook-creation`), deleted after the hook phase succeeds, kept after a failure for debugging. | requires `networkPolicy.enabled` and non-empty `networkPolicy.egress.postgres.cidrs` |
| `preprovisioned` | An operator-managed policy already lets pods labelled `app.kubernetes.io/component=migration` reach PostgreSQL. Its name is recorded on the Job (`oan.coss/migration-network-policy`); Helm cannot verify it. | requires `migration.network.preprovisionedPolicy` |
| `unrestricted` (default) | The namespace has no egress restriction for this pod. | refused when `networkPolicy.egress.enabled` is true |

  NetworkPolicy enforcement by the CNI may lag a few seconds behind object creation. With
  `backoffLimit: 0` a first-connection failure fails the upgrade without having changed
  the schema; raise `migration.backoffLimit` if this is seen on the target cluster.

### 3.4 Retention and cleanup execution

`run_cleanup` first recovers stale `sending` rows (they become `unknown`, never resent),
then calls `purge_due` in bounded batches until a batch touches nothing. Every step is
conditional SQL, idempotent and safe to overlap, and it resumes after an outage. The
retention rules themselves are enforced by the accepted storage (content purge, binding
release after the guarantee window, receipt fingerprint and evidence expiry).

| Mode | Resource | Runs |
|---|---|---|
| `cleanup.mode: cronjob` (default) | CronJob, `concurrencyPolicy: Forbid`, `backoffLimit: 0`, `activeDeadlineSeconds: 600` | one pass per `cleanup.schedule` (default `*/5 * * * *`, matching `purge_sweep_interval` = 5 min) |
| `cleanup.mode: deployment` | Deployment ×1 | `cleanup_forever`, every `purge_sweep_interval` |

The policy's target, content gone within `worker_lease + purge_sweep_interval`, assumes
the schedule matches `config.policy.purgeSweepInterval`. Keep them aligned. Key retirement
is never automatic (`retire_key_version` refuses while data still uses a version).

### 3.5 Non-root, least-privilege workloads

* Pods: `runAsNonRoot`, uid/gid `10001` (the image must contain it), seccomp
  `RuntimeDefault`, `automountServiceAccountToken: false`, `enableServiceLinks: false`.
* Containers: no privilege escalation, read-only root filesystem (`/tmp` is a 64 Mi
  emptyDir), all capabilities dropped, requests and limits on every container including
  the init container. `PYTHONDONTWRITEBYTECODE=1`.
* Secrets: referenced by name and key only (`secretKeyRef`), per component (§2). No
  `envFrom` on a Secret, so a component never receives keys it does not use.
* NetworkPolicy (`networkPolicy.enabled: true`):
  * API ingress only from `networkPolicy.api.from` (empty means nothing can reach it);
  * receipt ingress only from `networkPolicy.receipts.fromCIDRs` (8028 source ranges);
  * no ingress at all to the worker, cleanup and migration pods;
  * optional egress allow-list (`networkPolicy.egress.enabled`, off by default until the
    destinations are known): DNS + PostgreSQL for all pods, and 8028 (`:3400`) and
    Telegram (`:443`) for workers only.

### 3.6 Probes and graceful shutdown

* **API:** HTTP probes. Startup and liveness use `/livez` (the process serves requests);
  readiness uses `/readyz` (the database answers), so a pod without database access is
  taken out of the Service.
* **Receipts:** `tcpSocket` probes on the TLS port. A bare TCP connect never completes a
  TLS handshake; check on the cluster whether asyncio logs each probe as a handshake
  error, and disable the probes (`receipts.probes: {}`) if it does.
* **Worker:** an exec liveness probe, `python -m oan_messaging.runtime check-heartbeat
  --max-age 90`. The worker touches a heartbeat file after every cycle, success or
  handled failure; a hung loop stops touching it and the pod is restarted.
* **Cleanup and status check:** CronJobs; a failed run shows as a failed Job. The status
  check fails (exit 2) on any alert, so alerting can watch failed Jobs.
* **Shutdown:** uvicorn handles SIGTERM (`--timeout-graceful-shutdown 20`, grace period
  30 s). The worker stops on SIGTERM after its current cycle.
  `worker.terminationGracePeriodSeconds` (default 150) must be ≥ the worker lease (default
  120 s, `config.policy.workerLease`), and rendering fails otherwise. A worker that does
  not finish within the lease leaves its claim to lease recovery: an interrupted
  `sending` becomes `unknown`, never a resend.

### 3.7 Connection estimates and budget

The chart computes two **estimates from configured limits**. Nothing at runtime enforces
them: they are only as good as the values (and the MSG-08 runtime honouring them).

* Per pod: `pool.size + pool.maxOverflow` (`OAN_MSG_DB_POOL_SIZE`,
  `OAN_MSG_DB_MAX_OVERFLOW`); workers add `worker.coordinationConnections` for the rate
  limiter's own non-pooled connections.
* **Steady state:** every enabled pod at its limit; one cleanup pod (CronJob with
  `concurrencyPolicy: Forbid`, or a Deployment with `strategy: Recreate`); no migration.
* **Upgrade/rollout peak:** per Deployment `(2 × replicas + maxSurge)` pods. The
  controller keeps at most `replicas + maxSurge` non-terminating pods, but terminating
  pods are not counted and keep their connections until they exit (bounded by
  `terminationGracePeriodSeconds`: 30 s API/receipts, 150 s workers), so up to `replicas`
  old pods can still be connected. All Deployments may roll at once. Plus one cleanup pod
  and **1** for the migration hook, which runs while the previous workloads are still up.

```text
steady  = Σ replicas × perPod            + cleanup.perPod
rollout = Σ (2 × replicas + maxSurge) × perPod + cleanup.perPod + 1 (migration)
```

| Configuration | Steady | Rollout |
|---|---|---|
| defaults (API only + migration) | 20 | 51 |
| all components (`ci/02`) | 54 | 133 |

Both figures are printed in the install NOTES. When `database.connectionBudget` > 0,
rendering fails if the **rollout** estimate exceeds it (the regression checks cover the
exact boundary: 133 renders, 132 is refused). Inputs are validated as integers:
`replicas ≥ 0`, `rollout.maxSurge ≥ 0`, `pool.size ≥ 1`, `pool.maxOverflow ≥ 0`
(SQLAlchemy treats 0 and −1 as unlimited), `coordinationConnections ≥ 1`, budget ≥ 0.

Not bounded by the estimate: a pod that is stuck terminating beyond its grace period
(force deletion, node failure), manual `kubectl` scaling, `kubectl rollout restart` of
several components while an upgrade is also running, and connections of other
applications sharing the server.

`worker.coordinationConnections` is a **budget input only**: no setting passes it to the
runtime. The MSG-08 worker entrypoint must construct `SharedRateLimiter` with the same
`max_coordination_connections` (code default 2).

**No transaction-mode PgBouncer in front of the workers.** The rate limiter relies on
session advisory locks (`pg_try_advisory_lock`), which a transaction-mode pooler breaks.
Session mode, or direct connections, are fine.

### 3.8 Certificate and key rotation

| Material | Rotation |
|---|---|
| 8028 client certificate/key (`secrets.sms8028.tlsSecret`) | Update certificate and key **in one Secret update** (one generation), then `kubectl rollout restart deployment/<fullname>-worker`. The snapshot is taken by the init container at pod start; running pods keep their published pair until restarted. A Secret updated in two steps (certificate, then key) produces an intermediate mismatched generation; pods starting at that moment fail validation and retry rather than run with it. |
| Receipt server certificate (`secrets.receipts.serverTlsSecret`) | Same, for the receipts Deployment. Tell 8028 in advance if it pins our server certificate. |
| 8028's callback CA (`secrets.receipts.clientCaSecret`) | Add the new CA as a second key in `clientCaKeys`, roll out, remove the old one after 8028 switches. Read at startup only. |
| Pinned 8028 callback certificates (`secrets.receipts.pinnedCertsSecret`, optional) | Add the new certificate as a second key (`pinnedCertKeys: [current.pem, previous.pem]`), roll out, wait until 8028 has switched, then remove the old one and roll out again. `load_pins` reads pins at startup only. |
| Callback secret (`secrets.receipts.callbackSecret`) | New value under a new key; set `key` to it and `previousKey` to the old key; roll out; after 8028 switches, clear `previousKey` and roll out again. |
| Storage keys (`OAN_MSG_CONTENT_KEYS`, `_FINGERPRINT_KEYS`) | Keyrings: add the new version, change `config.contentKeyCurrent` / `fingerprintKeyCurrent`, roll out all components together (one ConfigMap), and retire the old version only when `retire_key_version` allows it. |
| Caller credential pepper, database passwords | Update the Secret and restart; a pepper change invalidates every issued caller credential. |

Changing a Secret does not restart pods on its own: the chart does not own the Secrets,
so it cannot add a checksum annotation for them. ConfigMap changes do restart pods
(`checksum/config`).

## 4. Configuration

### 4.0 Names

Names are never truncated. Truncation could erase the component suffix and make two
resources (including the migration hook's) collide. The prefix (`fullnameOverride`, or
`<release>-<chart or nameOverride>`) must be a DNS-1123 label of **at most 39
characters**. That leaves room for the longest suffix, `-worker-providers-egress` (24),
under the 63-character Service limit and the 52-character CronJob limit. Longer or
invalid prefixes fail rendering with a message asking for a shorter `fullnameOverride`;
`nameOverride` must be a DNS-1123 label of at most 63 characters. The regression checks
render every values file with a 53-character release name (Helm's maximum), with
`nameOverride`, with a 39-character `fullnameOverride`, and with a release containing the
chart name. For each they assert:

* every kind/name is unique across hook and regular resources;
* names and label values stay within their limits;
* every workload keeps its component suffix;
* every Service, PodDisruptionBudget and component NetworkPolicy selects exactly its own
  component's pods.

### 4.1 Secrets to create before installing (operators, out of band)

| Values key | Type | Keys |
|---|---|---|
| `database.existingSecret` | Opaque | `runtime-url`: `postgresql+psycopg://<dml-role>:<password>@<host>:5432/<db>?sslmode=verify-full` |
| `database.migration.existingSecret` (defaults to the one above) | Opaque | `migration-url` (DDL/owner role, same format) |
| `secrets.core.existingSecret` | Opaque | `content-keys` (`v1:<base64 32 bytes>[,v0:…]`), `fingerprint-keys` (`k1:<base64 ≥32 bytes>[,…]`), `credential-pepper` (`<base64 ≥32 bytes>`) |
| `secrets.sms8028.existingSecret` | Opaque | `client-secret` |
| `secrets.sms8028.tlsSecret` | kubernetes.io/tls | `tls.crt`, `tls.key` (8028-issued client certificate); optional CA key named by `caKey` |
| `secrets.telegram.existingSecret` | Opaque | one key per bot, mapped in `secrets.telegram.bots` (bot key `^[a-z0-9][a-z0-9_-]{0,63}$`) |
| `secrets.receipts.serverTlsSecret` | kubernetes.io/tls | `tls.crt`, `tls.key` (server certificate 8028 connects to) |
| `secrets.receipts.clientCaSecret` | Opaque | 8028's callback CA, PEM, named in `clientCaKeys` (8028 guide v1.1) |
| `secrets.receipts.pinnedCertsSecret` (optional) | Opaque | PEM files named in `pinnedCertKeys`; set `pinnedCertKeys: []` to rely on the CA alone |
| `secrets.receipts.callbackSecret.existingSecret` | Opaque | the callback secret 8028 sends in every callback (`key`, default `callback-secret`; optional `previousKey`) |

Never commit these, put them in values files, or pass them with `--set`.

### 4.2 Values to set for a dev environment

```yaml
image: {repository: <registry>/oan-messaging, digest: "sha256:<MSG-08 image>"}
image: {workingDir: <directory holding migrations/>}        # MSG-08 image layout
database: {connectionBudget: <share of max_connections>}
networkPolicy:
  api: {from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: <gateway ns>}}}]}
  receipts: {fromCIDRs: [213.55.73.209/32]}   # 8028's callback source (20 Nov 2026)
secrets:
  receipts:
    clientCaSecret: oan-messaging-8028-callback-ca     # 8028 guide v1.1: trust its CA
    clientCaKeys: [ca.crt]
    pinnedCertKeys: []                                 # no pins unless 8028 sends one
receipts:
  path: /sms-status-receiver/v1/update-status          # registered with 8028
  service:
    annotations: {<L4 load balancer annotations; no TLS termination>}
    loadBalancerSourceRanges: [213.55.73.209/32]
migration:
  network: {mode: hook}      # or preprovisioned + preprovisionedPolicy, see §3.3
networkPolicy:
  egress: {enabled: true, postgres: {cidrs: [<PostgreSQL CIDR>]}}
```

Install with the pilot overlay, which turns on the worker, receipt ingress, cleanup and
status check with their real commands:

```bash
helm upgrade --install oan-msg deploy/helm/oan-messaging \
  -f deploy/helm/oan-messaging/values-pilot.yaml -f <environment-values>.yaml
```

After the first install, apply the runtime role's grants (`deploy/sql/grants.sql`, as
the owner role); until then the worker and receipt pods are refused by the database and
restart. See `docs/runbook.md` for the operator commands (callers, tokens, suspend and
resume, status).

The API itself is exposed by the cluster gateway (for example an APISIX route to
`<release>-oan-messaging-api:80`, `/api/v1/messages/*` only), or with `api.ingress`.
8028 allow-lists our **egress** IP for the gateway calls (`:3400`), so the worker nodes
need a fixed egress IP (NAT gateway or egress gateway). The chart cannot provide that.

Representative values files used for validation: `deploy/helm/oan-messaging/ci/`
(`02-all-components-proposed-values.yaml` and `04-status-check-values.yaml` render every
template with the real commands).

## 5. Validation

| Date | Check | Result |
|---|---|---|
| 16 November 2026 | `ci/run-regressions.sh` (lint, render, secret scan, kubeconform) with helm v4.3.1, kubeconform v0.8.0 | All pass; **2,824 render checks**, 0 failed |
| 15 November 2026 | Install on a throwaway local kind cluster with `values-pilot.yaml`: migration hook, grants, probes, cleanup Job, status CronJob (including a deliberate `suspended:sms` alert), worker pod replacement, `helm upgrade` to a new image | All pass |

Every regression check is repeatable with one command:

```bash
PYTHON=<python with PyYAML> deploy/helm/oan-messaging/ci/run-regressions.sh
```

Not checked: CNI NetworkPolicy enforcement (kindnet does not enforce it), cloud load
balancer behaviour, Pod Security Admission, multi-node scheduling, and the receipt probe
log behaviour in §3.6.

## 6. Assumptions

* The image (repository `Dockerfile`) runs as uid/gid 10001, has `python`, `uvicorn` and
  `alembic` on `PATH`, and has `migrations/` under `image.workingDir`.
* PostgreSQL is reachable over TLS and its CA is trusted by the image.
* 8028 can reach a public TCP endpoint. Its callback source is 213.55.73.209 (8028,
  20 Nov 2026). Send 8028 the receipt URL of each environment (they asked for it).
* All components share one ServicePolicy (one ConfigMap) and roll out together after a
  policy change.

## 7. Blockers and open items

**Resolved by MSG-08:** runtime image, worker, receipt and cleanup entry points, API
health routes, worker heartbeat, status check, start-up checks and the least-privilege
grants (`deploy/sql/grants.sql`).

**Still open:**

1. `worker.coordinationConnections` is a budget input only: keep it equal to the rate
   limiter's coordination connections in the runtime (default 2).
2. Optional database-level migration guard (for example an advisory lock in
   `migrations/env.py`) if more than one release or manual migrations could ever target the
   same database (§3.3).
3. A phased rollout and stop sequence for upgrades that change behaviour across versions
   (release blocker OI-72).
4. Independent review of the service before this chart is used outside a disposable dev
   namespace.

**Operations / open questions:**

* Which cluster hosts OAN Messaging, and its API hostname (no dev URL exists).
* A fixed egress IP for 8028, and 8028's callback source CIDRs.
* The L4 load balancer type on the target cluster (NLB, MetalLB, ingress-nginx
  pass-through or APISIX stream route).
* Which certificate 8028 presents on callbacks (CA-issued or fixed) and its source addresses;
  routing of the registered callback host (`iiss.et`) to the receipts Service.
* Database: a dedicated database with separate DDL and DML roles, `max_connections`
  share, and no transaction-mode pooler.
* Whether the API sits behind APISIX (an `ApisixRoute` is not templated here, to avoid a
  CRD dependency).
