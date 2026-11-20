# Reproducing the integration trial

Local Docker only. Everything uses trial credentials generated on this machine and test
numbers (`0900000xxx`). The provider is a fake 8028 gateway, so nothing leaves the Docker
network.

## 1. OAN stack (API over HTTPS, worker, receipts, cleanup, fake 8028)

```
cd stack && ./init-env.sh && docker compose up -d --build
docker compose run --rm -T --entrypoint cat fake-8028 /certs/ca.crt > trial-ca.crt
python tools/trial.py smoke        # accepted -> gateway -> receipt -> delivered
```

* `tools/gen_certs.py` creates a throwaway CA and the certificates. They carry key
  identifiers so that strict TLS checks in Python 3.13 and later pass.
* `tools/fake8028.py` is the gateway. `http://127.0.0.1:18028/inbox` shows what it
  "sent".
* Register callers with `python -m oan_messaging.runtime caller …` and
  `issue-credential …` (see `tools/trial.py onboard`).

## 2. Credit (Frappe v16 + `oan_messaging_connector` + patched `oan_a2c`)

```
cd stack/a2c && echo "A2C_DB_ROOT_PW=$(openssl rand -hex 16)" > .env && docker compose up -d --build
```

Inside `a2c-bench`:
1. `bench init --frappe-branch version-16 --skip-redis-config-generation --skip-assets --python python3`.
   The image has no Redis server; the compose file provides Redis containers, so bench
   must not generate its own Redis configuration.
   Then point bench at the compose services:
   `bench set-config -g db_host a2c-mariadb`,
   `bench set-config -g redis_cache redis://a2c-redis-cache:6379`,
   `bench set-config -g redis_queue redis://a2c-redis-queue:6379` and
   `bench set-config -g redis_socketio redis://a2c-redis-cache:6379`.
2. `bench get-app` for `/repos/oan_auth_service`, `/repos/oan_messaging_connector` and
   `/repos/oan_a2c_service` (with the Credit kit applied; see `trial/repos/README.md`).
3. `bench new-site a2c.trial`, then install `oan_a2c` and `oan_messaging_connector`.
4. `set-config oan_messaging_enabled / _base_url https://oan-api:8000 / _token /
   _ca_bundle`.
5. Start `bench worker --queue short,default` and `bench schedule`, using
   `docker compose exec -d` so they keep running.

Run `sites/e2e_a2c.py` (`e2e/e2e_a2c.py`), then `e2e/poll_wait.py`.

The other Frappe kits run on the same bench, each on its own site with its own caller
(`transactional` and `otp`) and the same four settings. Install the app on the bench
**before** starting the worker (a running worker cannot import an app added later):

* Auth kit: `e2e/e2e_auth_reset.py` on `a2c.trial` (the `a2c` caller needs `otp` too).
* Grievance kit: `bench new-site grievance.trial`, install `oan_auth_service`,
  `oan_messaging_connector` and `oan_grievance_service`; run `e2e/e2e_grievance.py`.
* DA kit: `bench new-site da.trial`, install `oan_auth_service`, `oan_messaging_connector`
  and `da_services`; run `bench --site da.trial execute da_services.setup.install.before_tests`
  once for the test geography, then `e2e/e2e_da.py`.

Copy each script into `sites/` and run it with `../env/bin/python <script>`.

Tests: `bench --site a2c.trial run-tests --app oan_messaging_connector` and
`--app oan_a2c`.

## 3. Consent Manager (patched, OAN delivery on)

1. Build the image from `repos/consent-management` with
   `--build-arg FASTAPI_COMMON_REF=9ddccc00f502c49415c0220e72d17a4a8edc3c5a`. This is the
   last framework commit the code works with.
2. Build `Dockerfile.trial-fix`, which adds the missing `greenlet`.
3. `cd stack/consent`, then create `.env` with `CM_DB_PW` and
   `CONSENT_MANAGER_OAN_DELIVERY_ENABLED=true`, `CONSENT_MANAGER_OAN_TOKEN` and
   `CONSENT_MANAGER_OAN_OTP_PHONE_MAP`.
4. `docker compose up -d`, then `python e2e_consent.py`.
