# OAN Messaging Service

Shared outbound delivery for OAN services: 8028 SMS and Telegram. It provides durable
acceptance, idempotent submission, an outbound worker with a shared rate limit, and
authenticated 8028 delivery receipts. The external contract is **3.0.0**
(`docs/openapi.yaml`).

Status: **Review, not released.** The service is in independent review; review records
are kept with the project and are not part of developer exports. Licensed under the MIT
License, the same as other OAN services (see `LICENSE`).

## Layout

| Path | Contents |
|---|---|
| `oan_messaging/` | API, storage, adapters, worker, receipts and the `runtime/` entry points |
| `migrations/` | Alembic migrations (run as the owner role) |
| `deploy/sql/grants.sql` | Least-privilege runtime role grants |
| `deploy/helm/oan-messaging/` | Helm chart; `values-pilot.yaml` enables all components |
| `Dockerfile`, `deploy/docker/requirements.txt` | Non-root runtime image; dependencies hashed from `uv.lock` |
| `examples/` | Python client (`oan_client/`) and one sample per calling module; `telegram_notify.py` and `rapidpro_mapping.py` are future scope only |
| `docs/` | Architecture, contract (`openapi.yaml`), 8028 contract notes, operations runbook, Helm guide |
| `docs/channels-spec/` | **OAN Channels Specification v2.0** (SMS and Telegram), Markdown and Word |
| `docs/handoff/` | **Integration handoff** for module teams, Markdown and Word; QA cases |
| `docs/evidence/` | Release-blocker status; the RapidPro evaluation |
| `integrations/kits/` | Integration kits: one tested patch per module (Credit, Consent Manager, Auth, Grievance, DA) and a Java OTP sender for eSignet, each with its README |
| `trial/` | Local integration trial with a fake 8028 gateway (`trial/README.md`) |

The Frappe app that other OAN Frappe modules install is in the separate
`oan_messaging_connector` repository.

## Develop and test

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and a **local** PostgreSQL 16 for
tests. Providers are always mocked in tests, and the tests refuse non-loopback network
connections.

```
uv sync --frozen --extra dev
export OAN_MSG_TEST_DATABASE_URL=postgresql+psycopg://postgres:<local password>@127.0.0.1:5432/postgres
PYTHONPATH=.:examples uv run pytest tests -q
uv run ruff check . && uv run mypy -p oan_messaging
deploy/helm/oan-messaging/ci/run-regressions.sh
```

The tests create and drop their own randomly named databases. Never point
`OAN_MSG_TEST_DATABASE_URL` at a shared or production server.

## Run locally

```
deploy/local/dev-env.sh        # writes .env with generated development credentials
docker compose up --build      # PostgreSQL, migrations, API on 127.0.0.1:8000
curl http://127.0.0.1:8000/readyz
```

No worker runs locally, so nothing is sent. To try the API, onboard a test caller
(note that `docker compose run` prints the token):

```
docker compose run --rm api python -m oan_messaging.runtime caller --caller demo --channels sms --purposes transactional
docker compose run --rm api python -m oan_messaging.runtime issue-credential --caller demo
```

## Deploy

See `docs/runbook.md` (set-up, monitoring, procedures) and `docs/deployment/helm.md`.
Hosting endpoints are not defined. Every host in the chart and the docs is an
`example.invalid` placeholder.
