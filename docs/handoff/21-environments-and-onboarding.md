# 21 Environments and onboarding

## 1. Register your service (once per environment)

The **OAN operator** creates a caller per service. Ask for the channels and purposes you
need, and nothing more.

```
python -m oan_messaging.runtime caller --caller a2c --channels sms --purposes transactional
python -m oan_messaging.runtime issue-credential --caller a2c       # prints the token ONCE
python -m oan_messaging.runtime list-credentials --caller a2c       # ids and dates, never tokens
python -m oan_messaging.runtime revoke-credentials --caller a2c --credential <id>
```

| Caller | Channels | Purposes |
|---|---|---|
| `a2c` | sms | transactional, otp (Auth reset codes on this site) |
| `dpi-consent` | sms | otp, transactional |
| `grievance` | sms | transactional, otp (Auth reset codes on this site) |
| `da` | sms | transactional, otp (Auth reset codes on this site) |
| `esignet` | sms | otp |
| (none in Phase 1) | telegram | transactional |

* The token goes straight into **your secret store**; it cannot be shown again.
* **Rotate** by issuing a new token, switching over, then revoking the old one by its id.

## 2. Configuration per environment

| Setting | Frappe connector (site config) | Consent Manager (env) | eSignet, others |
|---|---|---|---|
| Enable | `oan_messaging_enabled` | `CONSENT_MANAGER_OAN_DELIVERY_ENABLED` | own flag |
| Base URL (HTTPS) | `oan_messaging_base_url` | `CONSENT_MANAGER_OAN_BASE_URL` | own config |
| Token (secret) | `oan_messaging_token` | `CONSENT_MANAGER_OAN_TOKEN` | own secret store |
| Private CA bundle | `oan_messaging_ca_bundle` | `CONSENT_MANAGER_OAN_CA_BUNDLE` | own trust store |

**Environment URLs are not defined yet.** Every host in these documents is an
`example.invalid` placeholder. DevOps publishes the real dev and staging URLs, and the
effective expiry ceilings.

## 3. TLS requirements (learned in the trial)

* **The OAN API must be HTTPS.** The client refuses `http://`. (The `servers` entry in
  `openapi.yaml` is a local mock address; do not generate a client default from it.)
* **Python 3.13 and newer verify certificates strictly.** Frappe v16 runs **Python 3.14**.
  If OAN uses a private CA:
  * the CA certificate needs **Basic Constraints CA:true**, **Key Usage
    (keyCertSign, cRLSign)** and a **Subject Key Identifier**;
  * the server certificate needs a **Subject Key Identifier**, an **Authority Key
    Identifier**, a **Subject Alternative Name** for the host you call, and **Extended
    Key Usage serverAuth**.

  Certificates without these fail with "Missing Authority Key Identifier", even though
  older Python clients accept them.
* Frappe v16 bench images need the MariaDB client headers (`libmariadb-dev`) to build
  `mysqlclient`.

## 4. Network

| From | To | Port |
|---|---|---|
| Your service | OAN API | 443 (HTTPS) |
| OAN worker | 8028 gateway | 8028's port, mTLS |
| 8028 | OAN receipt ingress (registered callback host) | 443 (mTLS with 8028's callback CA, plus callback secret) |

Callers never talk to 8028 or Telegram directly. (The Telegram bot is not an OAN caller: RapidPro talks to Telegram itself.)
