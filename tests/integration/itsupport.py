"""MSG-07 integration harness: the REAL accepted API, PostgreSQL repository, worker,
rate limiter, 8028/Telegram adapters and receipt ingress, composed in one process.

Only provider HTTP is mocked:

* ``Fake8028`` / ``FakeTelegramApi`` are ``httpx.MockTransport`` handlers injected into the
  real adapters' HTTP clients. Every adapter code path above the socket runs unchanged.
* ``TlsFakeProvider`` (process tests and log tests) is a loopback HTTPS server, so the real
  httpx/httpcore stack, server verification and 8028 mTLS run end to end.

Numbers use the controlled 0900000xxx range. All keys, credentials, bot tokens and
certificates are generated per run; nothing here is a real secret or endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import secrets
import ssl
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from oan_messaging.adapters.sms8028 import EAT, Sms8028Adapter, Sms8028Settings
from oan_messaging.adapters.telegram import TelegramAdapter, TelegramBotRegistry
from oan_messaging.api import ApiDependencies, create_app
from oan_messaging.contracts import (
    DEFAULT_POLICY,
    Channel,
    HmacFingerprinter,
    Purpose,
    ServicePolicy,
)
from oan_messaging.receipts import CallbackServer, ReceiptProcessor, build_server_ssl_context
from oan_messaging.receipts.ingress import load_pins
from oan_messaging.storage.credentials import authenticate, issue_credential, upsert_caller
from oan_messaging.storage.crypto import ContentCipher
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.worker import RateLimit, SharedRateLimiter, Worker, WorkerConfig
from oan_messaging.worker.rate_limit import clock_time_source

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)
ALL_TABLES = (
    "dispatch_controls, receipts, message_attempts, message_content, messages, api_credentials, "
    "callers, retired_key_versions, stats_daily, stats_failures, stats_campaigns, stats_state"
)
OTP_TEXT = "Your OAN code is 482913."
SMS_BASE_URL = "https://8028.invalid/sms-api/v1"
TG_BASE_URL = "https://api.telegram.invalid"


def libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def create_database(admin: str, prefix: str = "oan_msg_itest") -> str:
    name = f"{prefix}_{secrets.token_hex(4)}"
    with psycopg.connect(libpq(admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return admin.rsplit("/", 1)[0] + "/" + name


def drop_database(admin: str, url: str) -> None:
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(libpq(admin), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def fake_number(i: int) -> str:
    """Controlled test range 0900000xxx only."""
    assert 0 <= i <= 999
    return f"0900000{i:03d}"


def e164(domestic: str) -> str:
    return "+251" + domestic[1:]


def new_bot_token() -> str:
    return f"{secrets.randbelow(10**9) + 10**8}:" + secrets.token_urlsafe(30)[:35]


# --------------------------------------------------------------------------- clock


@dataclass
class FakeClock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now

    def monotonic(self) -> float:
        return self.now.timestamp()

    async def sleep(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------- keys


@dataclass
class Keys:
    content: dict[str, bytes]
    fingerprint: dict[str, bytes]
    pepper: bytes
    sms_secret: str
    bot_tokens: dict[str, str]

    def repo(
        self, sf: async_sessionmaker[AsyncSession], policy: ServicePolicy = DEFAULT_POLICY
    ) -> PostgresMessageRepository:
        return PostgresMessageRepository(
            sf,
            cipher=ContentCipher(self.content, "c1"),
            fingerprinter=HmacFingerprinter(self.fingerprint, "f1"),
            policy=policy,
        )

    def env(self) -> dict[str, str]:
        """Service configuration for subprocesses (generated test values only)."""
        return {
            "OAN_MSG_CONTENT_KEYS": "c1:" + base64.b64encode(self.content["c1"]).decode(),
            "OAN_MSG_CONTENT_KEY_CURRENT": "c1",
            "OAN_MSG_FINGERPRINT_KEYS": "f1:" + base64.b64encode(self.fingerprint["f1"]).decode(),
            "OAN_MSG_FINGERPRINT_KEY_CURRENT": "f1",
            "OAN_MSG_CREDENTIAL_PEPPER": base64.b64encode(self.pepper).decode(),
        }

    def secret_needles(self) -> list[str]:
        out = [self.sms_secret, *self.bot_tokens.values()]
        out += [base64.b64encode(v).decode() for v in self.content.values()]
        out += [base64.b64encode(v).decode() for v in self.fingerprint.values()]
        out.append(base64.b64encode(self.pepper).decode())
        return out


def new_keys() -> Keys:
    return Keys(
        content={"c1": secrets.token_bytes(32)},
        fingerprint={"f1": secrets.token_bytes(32)},
        pepper=secrets.token_bytes(32),
        sms_secret="it8028-" + secrets.token_urlsafe(24),
        bot_tokens={"grievance": new_bot_token(), "da": new_bot_token()},
    )


async def make_caller(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    caller_id: str,
    *,
    channels: frozenset[Channel] = frozenset(Channel),
    purposes: frozenset[Purpose] = frozenset(Purpose),
    bot: str | None = "grievance",
    enabled: bool = True,
    stats_reader: bool = False,
) -> str:
    if stats_reader:
        channels, purposes, bot = frozenset(), frozenset(), None
    await upsert_caller(
        sf,
        caller_id,
        channels=channels,
        purposes=purposes,
        telegram_bot=bot,
        enabled=enabled,
        can_read_stats=stats_reader,
    )
    return await issue_credential(sf, keys.pepper, caller_id, T0)


# ------------------------------------------------------------- mocked provider HTTP

Handler = Callable[[dict[str, Any], int], Awaitable[httpx.Response] | httpx.Response]


@dataclass
class ProviderCall:
    at: datetime
    url: str
    body: dict[str, Any]


class Fake8028:
    """Mocked 8028 Send SMS endpoint. Default: success with a fresh numeric ``sms_id``.
    ``script(body, n)`` may return a response, raise an httpx exception, or be async."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock
        self.calls: list[ProviderCall] = []
        self.script: Handler | None = None
        self.returned_ids: list[str] = []
        self._seq = 7000

    def next_id(self) -> str:
        self._seq += 1
        return str(self._seq)

    def ok(self, body: dict[str, Any], sms_id: str | None = None) -> httpx.Response:
        sms_id = sms_id or self.next_id()
        self.returned_ids.append(sms_id)
        return httpx.Response(
            200,
            json={
                "success": True,
                "message": "queued",
                "data": [{"phone_number": body["phonenums"][0], "sms_id": sms_id}],
            },
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.calls.append(ProviderCall(self.clock(), str(request.url), body))
        if self.script is None:
            return self.ok(body)
        out = self.script(body, len(self.calls))
        if isinstance(out, httpx.Response):
            return out
        return await out


class FakeTelegramApi:
    """Mocked Telegram Bot API ``sendMessage``. Records which token (bot) was used."""

    def __init__(self, clock: Callable[[], datetime], tokens: dict[str, str]) -> None:
        self.clock = clock
        self.calls: list[ProviderCall] = []
        self.script: Handler | None = None
        self._token_to_bot = {v: k for k, v in tokens.items()}
        self._seq = 300

    def bot_of(self, call: ProviderCall) -> str | None:
        token = call.url.split("/bot", 1)[1].split("/", 1)[0]
        return self._token_to_bot.get(token)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.calls.append(ProviderCall(self.clock(), str(request.url), body))
        if self.script is not None:
            out = self.script(body, len(self.calls))
            return out if isinstance(out, httpx.Response) else await out
        self._seq += 1
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": self._seq, "chat": {"id": body["chat_id"]}}},
        )


def sms_adapter(keys: Keys, fake: Fake8028, clock: Callable[[], datetime]) -> Sms8028Adapter:
    settings = Sms8028Settings(
        client_secret=SecretStr(keys.sms_secret),
        client_cert=Path("/nonexistent/client.crt"),  # TLS not used with MockTransport
        client_key=Path("/nonexistent/client.key"),
        base_url=SMS_BASE_URL,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle), follow_redirects=False)
    return Sms8028Adapter(settings, client, clock=clock)


def telegram_adapter(
    keys: Keys, fake: FakeTelegramApi, clock: Callable[[], datetime]
) -> TelegramAdapter:
    registry = TelegramBotRegistry({k: SecretStr(v) for k, v in keys.bot_tokens.items()})
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle), follow_redirects=False)
    return TelegramAdapter(registry, client, api_base_url=TG_BASE_URL, clock=clock)


# ------------------------------------------------------------------------- harness


@dataclass
class Harness:
    engine: AsyncEngine
    sf: async_sessionmaker[AsyncSession]
    keys: Keys
    clock: FakeClock
    repo: PostgresMessageRepository
    sms: Fake8028
    tg: FakeTelegramApi
    api: httpx.AsyncClient
    limiters: list[SharedRateLimiter] = field(default_factory=list)
    #: The real FastAPI application behind ``api`` (for other loopback transports, e.g.
    #: the accepted MSG-06 client in itclient.py).
    app: Any = None

    async def caller(self, caller_id: str, **kw: Any) -> str:
        return await make_caller(self.sf, self.keys, caller_id, **kw)

    def worker(
        self,
        worker_id: str = "w1",
        *,
        sms: bool = True,
        tg: bool = True,
        sms_limit: RateLimit | None = None,
        tg_limit: RateLimit | None = None,
        **cfg: Any,
    ) -> Worker:
        limiter = SharedRateLimiter(
            self.engine,
            {
                Channel.SMS: sms_limit or RateLimit(requests=1000),
                Channel.TELEGRAM: tg_limit or RateLimit(requests=1000),
            },
            time_source=clock_time_source(self.clock),  # one shared simulated domain
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )
        self.limiters.append(limiter)
        adapters: dict[Channel, Any] = {}
        if sms:
            adapters[Channel.SMS] = sms_adapter(self.keys, self.sms, self.clock)
        if tg:
            adapters[Channel.TELEGRAM] = telegram_adapter(self.keys, self.tg, self.clock)
        return Worker(
            self.repo,
            adapters,
            limiter,
            WorkerConfig(
                worker_id=worker_id, **({"batch_size": 20, "broadcast_enabled": True} | cfg)
            ),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    async def close(self) -> None:
        for limiter in self.limiters:
            await limiter.aclose()
        await self.api.aclose()

    # -- API helpers --------------------------------------------------------------

    async def post_sms(self, token: str, key: str | None = None, **body: Any) -> httpx.Response:
        return await self.api.post(
            "/api/v1/messages/sms", json=sms_body(self.clock, **body), headers=hdr(token, key)
        )

    async def post_tg(self, token: str, key: str | None = None, **body: Any) -> httpx.Response:
        return await self.api.post(
            "/api/v1/messages/telegram", json=tg_body(**body), headers=hdr(token, key)
        )

    async def status(self, token: str, mid: str) -> dict[str, Any]:
        r = await self.api.get(f"/api/v1/messages/{mid}", headers=hdr(token))
        assert r.status_code == 200, r.text
        return dict(r.json())

    # -- database inspection ------------------------------------------------------

    async def one(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.sf() as s:
            return (await s.execute(text(sql), params)).first()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        async with self.sf() as s:
            return list((await s.execute(text(sql), params)).all())


def build_harness(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    clock: FakeClock,
    policy: ServicePolicy = DEFAULT_POLICY,
) -> Harness:
    repo = keys.repo(sf, policy)

    async def auth(token: str) -> Any:
        return await authenticate(sf, keys.pepper, token)

    from oan_messaging.stats.charts import StatsService

    app = create_app(
        ApiDependencies(
            repository=repo, authenticate=auth, clock=clock, stats=StatsService(sf, clock=clock)
        )
    )
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )
    return Harness(
        engine,
        sf,
        keys,
        clock,
        repo,
        Fake8028(clock),
        FakeTelegramApi(clock, keys.bot_tokens),
        api,
        app=app,
    )


def hdr(token: str, key: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


def new_key() -> str:
    return str(uuid.uuid4())


def sms_body(clock: Callable[[], datetime] | None = None, **kw: Any) -> dict[str, Any]:
    now = clock() if clock is not None else T0
    body: dict[str, Any] = {
        "recipient": fake_number(101),
        "text": OTP_TEXT,
        "source_reference": "esignet:otp:evt-1",
        "purpose": "otp",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    body.update(kw)
    return {k: v for k, v in body.items() if v is not None}


def tg_body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "chat_id": 555000111,
        "text": "Grievance G-1 updated.",
        "source_reference": "grievance:G-1:u1",
        "purpose": "transactional",
    }
    body.update(kw)
    return {k: v for k, v in body.items() if v is not None}


# ---------------------------------------------------------------- 8028 responses


def http_status(code: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(code, json=body if body is not None else {"success": False})


def eat(moment: datetime) -> str:
    return moment.astimezone(EAT).strftime("%Y-%m-%d %H:%M:%S")


# -------------------------------------------------------------------- certificates


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


@dataclass
class Certs:
    dir: Path
    paths: dict[str, Path] = field(default_factory=dict)

    def __getitem__(self, k: str) -> Path:
        return self.paths[k]


def make_certs(tmp: Path) -> Certs:
    """Fake CA; pinned 8028 callback client cert; an unpinned sibling from the same CA;
    a self-signed rogue with the same CN; a loopback server cert; a client cert used by
    the 8028 adapter for mTLS to the fake gateway."""
    now = datetime.now(UTC)
    out = Certs(tmp)

    def cert(
        name: str,
        subject: str,
        key: ec.EllipticCurvePrivateKey,
        issuer: x509.Name | None,
        issuer_key: ec.EllipticCurvePrivateKey | None,
        *,
        ca: bool = False,
        server: bool = False,
    ) -> x509.Certificate:
        b = (
            x509.CertificateBuilder()
            .subject_name(_name(subject))
            .issuer_name(issuer or _name(subject))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
        )
        # Key identifiers and CA key usage: Python 3.13+ verifies strictly (X.509 strict).
        b = b.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        b = b.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key((issuer_key or key).public_key()),
            critical=False,
        )
        if ca:
            b = b.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            b = b.add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        if server:
            b = b.add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
        c = b.sign(issuer_key or key, hashes.SHA256())
        out.paths[f"{name}_cert"] = tmp / f"{name}.crt"
        out.paths[f"{name}_cert"].write_bytes(c.public_bytes(serialization.Encoding.PEM))
        out.paths[f"{name}_key"] = tmp / f"{name}.key"
        out.paths[f"{name}_key"].write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        os.chmod(out.paths[f"{name}_key"], 0o600)
        return c

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = cert("ca", "it-fake-8028-ca", ca_key, None, None, ca=True)

    def leaf(name: str, cn: str, **kw: Any) -> None:
        cert(name, cn, ec.generate_private_key(ec.SECP256R1()), ca.subject, ca_key, **kw)

    leaf("pinned", "it-fake-8028-callback")
    leaf("sibling", "it-fake-8028-other")
    leaf("server", "localhost", server=True)
    leaf("gwclient", "it-oan-8028-client")
    cert("rogue", "it-fake-8028-callback", ec.generate_private_key(ec.SECP256R1()), None, None)
    return out


def client_ctx(certs: Certs, who: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(certs["ca_cert"]))
    if who is not None:
        ctx.load_cert_chain(str(certs[f"{who}_cert"]), str(certs[f"{who}_key"]))
    return ctx


# ----------------------------------------------------------------- receipt ingress


@dataclass
class Ingress:
    server: CallbackServer
    port: int
    certs: Certs

    async def post(
        self,
        payload: dict[str, Any] | bytes,
        who: str | None = "pinned",
        extra_headers: str = "",
    ) -> tuple[int | None, bytes]:
        """Raw HTTP/1.1 over real TLS. Returns (status, body) or (None, b"") when the
        TLS handshake (client-certificate check) refuses the connection."""
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        head = (
            "POST /sms-status-receiver/v1/update-status HTTP/1.1\r\nHost: localhost\r\n"
            + extra_headers
            + f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n"
        ).encode()
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", self.port, ssl=client_ctx(self.certs, who), server_hostname="localhost"
            )
        except (ssl.SSLError, ConnectionError):
            return None, b""
        try:
            writer.write(head + data)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 10)
        except (ssl.SSLError, ConnectionError):
            return None, b""
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ssl.SSLError, ConnectionError):
                pass
        if not raw:
            return None, b""
        status = int(raw.split(b" ", 2)[1])
        return status, raw.split(b"\r\n\r\n", 1)[1]


async def start_ingress(
    repo: PostgresMessageRepository,
    certs: Certs,
    clock: Callable[[], datetime],
    pinned: tuple[str, ...] = ("pinned",),
) -> Ingress:
    pem = [certs[f"{p}_cert"] for p in pinned]
    processor = ReceiptProcessor(repo, load_pins(pem), clock=clock)
    ctx = build_server_ssl_context(certs["server_cert"], certs["server_key"], pem)
    server = CallbackServer(processor, ctx)
    port = await server.start("127.0.0.1", 0)
    return Ingress(server, port, certs)


def callback(sms_id: str, domestic: str, status: str, event: datetime) -> dict[str, Any]:
    return {
        "sms_id": sms_id,
        "phone_number": domestic,
        "status": status,
        "event_time": eat(event),
    }


# ------------------------------------------------- loopback HTTPS fake providers


class TlsFakeProvider:
    """Loopback HTTPS server standing in for 8028 or Telegram (thread). Requires a client
    certificate when ``require_client_cert`` (8028 mTLS). Behaviour per request:
    ``mode`` = "ok" | "hang" | "telegram". Records request paths and bodies in memory."""

    def __init__(self, certs: Certs, *, require_client_cert: bool, mode: str = "ok") -> None:
        self.mode = mode
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.got_request = threading.Event()
        self.release = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:  # never print request lines
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                outer.requests.append((self.path, body))
                outer.got_request.set()
                if outer.mode == "hang":
                    outer.release.wait(60)
                    return
                if outer.mode == "telegram":
                    payload = {
                        "ok": True,
                        "result": {"message_id": 41, "chat": {"id": body["chat_id"]}},
                    }
                else:
                    payload = {
                        "success": True,
                        "message": "queued",
                        "data": [{"phone_number": body["phonenums"][0], "sms_id": "991"}],
                    }
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(certs["server_cert"]), str(certs["server_key"]))
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(certs["ca_cert"]))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = int(self.httpd.server_address[1])
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


# -------------------------------------------------------------- database scanning


def dump_database_text(url: str) -> str:
    """Every row of every public table as JSON text (bytea shown as hex)."""
    chunks: list[str] = []
    with psycopg.connect(libpq(url)) as conn:
        tables = [
            r[0]
            for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ]
        for t in tables:
            for (row,) in conn.execute(f'SELECT row_to_json(x)::text FROM "{t}" x'):  # noqa: S608
                chunks.append(f"{t}: {row}")
    return "\n".join(chunks)


def needle_forms(domestic: str) -> list[str]:
    """Every textual form of a fake number that would reveal it in full."""
    national = domestic[1:]
    return [domestic, "+251" + national, "251" + national, national]


def hex_forms(text: str) -> list[str]:
    return [text.encode("utf-8").hex(), text.encode("utf-16-le").hex()]
