#!/usr/bin/env sh
# Generate a local-only .env with fresh random development credentials (MSG-08).
# Never reuse these values anywhere else; .env is git-ignored.
set -eu
if [ -e .env ]; then echo ".env already exists; remove it to regenerate" >&2; exit 1; fi
key() { python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'; }
PG_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
umask 077
cat > .env <<ENV
POSTGRES_PASSWORD=${PG_PASSWORD}
OAN_MSG_DATABASE_URL=postgresql+psycopg://postgres:${PG_PASSWORD}@postgres:5432/oan_messaging
OAN_MSG_CONTENT_KEYS=v1:$(key)
OAN_MSG_CONTENT_KEY_CURRENT=v1
OAN_MSG_FINGERPRINT_KEYS=k1:$(key)
OAN_MSG_FINGERPRINT_KEY_CURRENT=k1
OAN_MSG_CREDENTIAL_PEPPER=$(key)
ENV
echo "wrote .env (mode 600) with generated development credentials"
