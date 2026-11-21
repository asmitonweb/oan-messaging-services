"""Create this install's Secrets once (database password, OTP salt, test partner-realm key). Never prints a value."""

import base64
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

NS = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
API = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
CTX = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:  # noqa: S310 - in-cluster API
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def create(name, data):
    enc = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    status = call("POST", f"/api/v1/namespaces/{NS}/secrets", {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "cm-test"}}, "data": enc})
    if status not in (200, 201):
        sys.exit(f"creating Secret {name} failed: HTTP {status}")
    print(f"created Secret {name} (keys: {', '.join(sorted(data))})")


NAMES = ["cm-test-app", "cm-test-partner-key"]
missing = [n for n in NAMES if call("GET", f"/api/v1/namespaces/{NS}/secrets/{n}") != 200]
if not missing:
    print("all Secrets present; nothing to do")
    sys.exit(0)
if len(missing) != len(NAMES):
    sys.exit("some Secrets exist and some do not; delete the rest to regenerate")

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
jwk.update(kid="cm-test-1", use="sig", alg="RS256")
create("cm-test-app", {"db-password": secrets.token_urlsafe(24), "otp-salt": secrets.token_urlsafe(32)})
create("cm-test-partner-key", {
    "private.pem": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode(),
    "jwks.json": json.dumps({"keys": [jwk]})})
