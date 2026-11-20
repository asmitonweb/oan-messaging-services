"""Create the Secrets a registry-test install needs, once. Never prints a value.

Runs in the service image as a Helm hook. If every Secret already exists it does nothing, so
upgrades keep the generated passwords, keys and certificates. Throwaway test material only:
the 8028 side is the fake gateway, signed by a test CA generated here (gen_certs.py).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

NS = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
API = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
CTX = ssl.create_default_context(cafile=CA)
DB_HOST = os.environ["DB_HOST"]
CERTS = Path("/tmp/certs")


def call(method: str, path: str, body: dict | None = None) -> int:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=20) as r:  # noqa: S310 - in-cluster API
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def exists(name: str) -> bool:
    return call("GET", f"/api/v1/namespaces/{NS}/secrets/{name}") == 200


def create(name: str, data: dict[str, str | bytes], kind: str = "Opaque") -> None:
    enc = {k: base64.b64encode(v if isinstance(v, bytes) else v.encode()).decode() for k, v in data.items()}
    status = call("POST", f"/api/v1/namespaces/{NS}/secrets", {
        "apiVersion": "v1", "kind": "Secret", "type": kind,
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "oan-messaging-test"}},
        "data": enc})
    if status not in (200, 201):
        sys.exit(f"creating Secret {name} failed: HTTP {status}")
    print(f"created Secret {name} (keys: {', '.join(sorted(data))})")


NAMES = ["oan-test-postgres", "oan-messaging-database", "oan-messaging-core", "oan-messaging-sms8028",
         "oan-messaging-sms8028-client-tls", "oan-messaging-8028-callback", "oan-messaging-8028-callback-ca",
         "oan-messaging-receipts-server-tls", "oan-test-fake8028"]
missing = [n for n in NAMES if not exists(n)]
if not missing:
    print("all Secrets present; nothing to do")
    sys.exit(0)
if len(missing) != len(NAMES):
    sys.exit("some Secrets exist and some do not: " + ", ".join(missing) + " missing. Delete the rest to regenerate.")

subprocess.run([sys.executable, "/scripts/gen_certs.py", str(CERTS)], check=True)
f = lambda n: (CERTS / n).read_bytes()  # noqa: E731
b64 = lambda n: base64.b64encode(secrets.token_bytes(n)).decode()  # noqa: E731
pw = {k: secrets.token_urlsafe(24) for k in ("postgres", "owner", "runtime")}
client_secret, callback_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

create("oan-test-postgres", {"postgres-password": pw["postgres"], "owner-password": pw["owner"],
                             "runtime-password": pw["runtime"]})
create("oan-messaging-database", {
    "runtime-url": f"postgresql+psycopg://oan_msg_runtime:{pw['runtime']}@{DB_HOST}:5432/oan_messaging",
    "migration-url": f"postgresql+psycopg://oan_msg_owner:{pw['owner']}@{DB_HOST}:5432/oan_messaging"})
create("oan-messaging-core", {"content-keys": "v1:" + b64(32), "fingerprint-keys": "k1:" + b64(32),
                              "credential-pepper": b64(32)})
create("oan-messaging-sms8028", {"client-secret": client_secret})
create("oan-messaging-sms8028-client-tls", {"tls.crt": f("oan-8028-client.crt"), "tls.key": f("oan-8028-client.key"),
                                            "ca.crt": f("ca.crt")}, "kubernetes.io/tls")
create("oan-messaging-8028-callback", {"callback-secret": callback_secret})
create("oan-messaging-8028-callback-ca", {"ca.crt": f("ca.crt")})
create("oan-messaging-receipts-server-tls", {"tls.crt": f("receipts-server.crt"), "tls.key": f("receipts-server.key")},
       "kubernetes.io/tls")
create("oan-test-fake8028", {"ca.crt": f("ca.crt"), "fake8028-server.crt": f("fake8028-server.crt"),
                             "fake8028-server.key": f("fake8028-server.key"), "callback-client.crt": f("callback-client.crt"),
                             "callback-client.key": f("callback-client.key"), "client-secret": client_secret,
                             "callback-secret": callback_secret})
