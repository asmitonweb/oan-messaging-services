# Local integration trial

Runs this service (API over HTTPS, worker, receipts, cleanup) and a **fake 8028
gateway** on your machine, plus optional Credit (A2C, Frappe v16) and DPI Consent Manager
stacks. Nothing leaves your machine; all numbers are `0900000xxx` test numbers.

## OAN only (every team)

```
cd trial/stack
./init-env.sh                        # generates trial passwords, keys and certificates
docker compose up -d --build         # builds this repository's image
docker compose run --rm -T --entrypoint cat fake-8028 /certs/ca.crt > trial-ca.crt
python tools/trial.py smoke          # accepted -> gateway -> receipt -> delivered
```

* Fake handset inbox: `http://127.0.0.1:18028/inbox`.
* OAN API: `https://localhost:18000` from your machine, `https://oan-api:8000` from
  containers on the `oan-trial` network. Trust `trial-ca.crt`.
* Your own caller and token: see `onboard()` in `tools/trial.py`.

## Credit or Consent Manager end to end

Prepare `trial/repos/` as `trial/repos/README.md` says, then follow `stack/README.md`
sections 2 and 3. `TRIAL-REPORT.md` records what the trial showed on 15 November 2026.

Generated files (`.env`, certificates, `trial-ca.crt`) and the checkouts in `repos/` are
ignored by git.
