#!/usr/bin/env sh
# Generate stack/.env with random, trial-only credentials (never reuse elsewhere).
set -eu
cd "$(dirname "$0")"
[ -e .env ] && { echo ".env exists"; exit 0; }
r() { python3 -c 'import secrets; print(secrets.token_hex(16))'; }
k() { python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'; }
umask 077
cat > .env <<ENV
POSTGRES_PASSWORD=$(r)
OAN_OWNER_PW=$(r)
OAN_RUNTIME_PW=$(r)
OAN_MSG_CONTENT_KEYS=v1:$(k)
OAN_MSG_CONTENT_KEY_CURRENT=v1
OAN_MSG_FINGERPRINT_KEYS=k1:$(k)
OAN_MSG_FINGERPRINT_KEY_CURRENT=k1
OAN_MSG_CREDENTIAL_PEPPER=$(k)
FAKE8028_CLIENT_SECRET=trial-$(r)
FAKE8028_CALLBACK_SECRET=trial-callback-$(r)
ENV
echo "wrote stack/.env"
