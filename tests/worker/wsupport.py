"""Shared helpers for MSG-05 tests: fake clock, keys, callers, fake providers, certificates.

Numbers use the 0900000xxx range; nothing is ever sent to a real provider.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from oan_messaging.contracts import (
    DEFAULT_POLICY,
    CallerContext,
    Channel,
    FailureCode,
    HmacFingerprinter,
    IdempotencyDecision,
    Provider,
    ProviderOutcome,
    ProviderResult,
    Purpose,
    SendRequest,
    ServicePolicy,
    SmsSubmission,
    StoredMessage,
    TelegramSubmission,
)
from oan_messaging.storage.credentials import authenticate, issue_credential, upsert_caller
from oan_messaging.storage.crypto import ContentCipher
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.worker import RateLimit, SharedRateLimiter, Worker, WorkerConfig
from oan_messaging.worker.rate_limit import clock_time_source

T0 = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)
ALL_TABLES = (
    "dispatch_controls, receipts, message_attempts, message_content, messages, api_credentials, "
    "callers, retired_key_versions, stats_daily, stats_failures, stats_campaigns, stats_state"
)
OTP_TEXT = "Your OAN code is 482913."


@dataclass
class FakeClock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now

    def monotonic(self) -> float:
        """Simulated monotonic seconds (advances only with this fake clock)."""
        return self.now.timestamp()

    async def sleep(self, seconds: float) -> None:
        """Injected worker sleep: advances fake time instead of waiting."""
        self.now = self.now + timedelta(seconds=seconds)


@dataclass
class Keys:
    content: dict[str, bytes]
    content_current: str
    fingerprint: dict[str, bytes]
    fingerprint_current: str
    pepper: bytes

    def repo(
        self, sf: async_sessionmaker[AsyncSession], policy: ServicePolicy = DEFAULT_POLICY
    ) -> PostgresMessageRepository:
        return PostgresMessageRepository(
            sf,
            cipher=ContentCipher(self.content, self.content_current),
            fingerprinter=HmacFingerprinter(self.fingerprint, self.fingerprint_current),
            policy=policy,
        )

    def rotated_fingerprint(self, version: str = "f2") -> Keys:
        return Keys(
            self.content,
            self.content_current,
            {**self.fingerprint, version: secrets.token_bytes(32)},
            version,
            self.pepper,
        )


def new_keys() -> Keys:
    return Keys(
        {"c1": secrets.token_bytes(32)},
        "c1",
        {"f1": secrets.token_bytes(32)},
        "f1",
        secrets.token_bytes(32),
    )


async def make_caller(
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    cid: str = "svc",
    *,
    bot: str | None = "grievance",
    enabled: bool = True,
    channels: frozenset[Channel] = frozenset(Channel),
    purposes: frozenset[Purpose] = frozenset(Purpose),
) -> CallerContext:
    await upsert_caller(
        sf, cid, channels=channels, purposes=purposes, telegram_bot=bot, enabled=enabled
    )
    token = await issue_credential(sf, keys.pepper, cid, T0)
    ctx = await authenticate(sf, keys.pepper, token)
    assert ctx is not None
    return ctx


async def submit_sms(
    repo: PostgresMessageRepository,
    ctx: CallerContext,
    *,
    at: datetime = T0,
    recipient: str = "0900000001",
    text: str = OTP_TEXT,
    purpose: str = "otp",
    expires_in: timedelta | None = timedelta(minutes=5),
    key: str | None = None,
) -> StoredMessage:
    body: dict[str, Any] = {
        "recipient": recipient,
        "text": text,
        "source_reference": "evt-1",
        "purpose": purpose,
    }
    if expires_in is not None:
        body["expires_at"] = (at + expires_in).isoformat()
    msg, decision = await repo.create_or_get(
        ctx, key or str(uuid.uuid4()), SmsSubmission.model_validate(body), at
    )
    assert decision is IdempotencyDecision.NEW
    return msg


async def submit_broadcast(
    repo: PostgresMessageRepository, ctx: CallerContext, i: int, *, at: datetime = T0
) -> StoredMessage:
    return await submit_sms(
        repo,
        ctx,
        at=at,
        recipient=f"09000{i:05d}",
        text=f"Advisory {i}",
        purpose="broadcast",
        expires_in=None,
    )


async def submit_telegram(
    repo: PostgresMessageRepository,
    ctx: CallerContext,
    *,
    at: datetime = T0,
    chat_id: int = 555000111,
) -> StoredMessage:
    sub = TelegramSubmission.model_validate(
        {
            "chat_id": chat_id,
            "text": "Grievance updated.",
            "source_reference": "g-1",
            "purpose": "transactional",
        }
    )
    msg, _ = await repo.create_or_get(ctx, str(uuid.uuid4()), sub, at)
    return msg


Script = Callable[[SendRequest, datetime], ProviderResult | Awaitable[ProviderResult]]


class FakeAdapter:
    """Mocked provider. ``script`` decides each result; every call is recorded."""

    provider: ClassVar[Provider]
    channel: ClassVar[Channel]

    def __init__(self, clock: FakeClock, script: Script | None = None) -> None:
        self.clock = clock
        self.script = script
        self.calls: list[SendRequest] = []
        self.counter: Counter[uuid.UUID] = Counter()
        self._seq = 1000

    async def send(self, request: SendRequest) -> ProviderResult:
        self.calls.append(request)
        self.counter[request.message_id] += 1
        now = self.clock()
        if self.script is not None:
            out = self.script(request, now)
            if isinstance(out, ProviderResult):
                return out
            return await out
        self._seq += 1
        return sent(self.provider, now, str(self._seq))


class FakeSms(FakeAdapter):
    provider = Provider.SMS_8028
    channel = Channel.SMS


class FakeTelegram(FakeAdapter):
    provider = Provider.TELEGRAM
    channel = Channel.TELEGRAM


def sent(provider: Provider, at: datetime, pid: str) -> ProviderResult:
    return ProviderResult(
        outcome=ProviderOutcome.SENT,
        provider=provider,
        provider_message_id=pid,
        provider_status="http_200",
        started_at=at,
        finished_at=at,
    )


def outcome(
    provider: Provider,
    at: datetime,
    kind: ProviderOutcome,
    code: FailureCode,
    status: str,
    *,
    retryable: bool = False,
    retry_after: int | None = None,
) -> ProviderResult:
    return ProviderResult(
        outcome=kind,
        provider=provider,
        provider_status=status,
        failure_code=code,
        retryable=retryable,
        retry_after_seconds=retry_after,
        started_at=at,
        finished_at=at,
    )


def make_worker(
    repo: PostgresMessageRepository,
    engine: AsyncEngine,
    clock: FakeClock,
    *,
    sms: FakeAdapter | None = None,
    tg: FakeAdapter | None = None,
    worker_id: str = "w1",
    sms_limit: RateLimit | None = None,
    tg_limit: RateLimit | None = None,
    sleep: Any = None,
    lock_sleep: Any = None,
    dispatch_tolerance: timedelta = timedelta(milliseconds=50),
    **cfg: Any,
) -> Worker:
    limiter = SharedRateLimiter(
        engine,
        {
            Channel.SMS: sms_limit or RateLimit(requests=1000),
            Channel.TELEGRAM: tg_limit or RateLimit(requests=1000),
        },
        dispatch_tolerance=dispatch_tolerance,
        time_source=clock_time_source(clock),  # explicit, shared simulated domain
        monotonic=clock.monotonic,
        sleep=lock_sleep or (sleep or clock.sleep),
    )
    adapters: dict[Channel, Any] = {}
    if sms is not None:
        adapters[Channel.SMS] = sms
    if tg is not None:
        adapters[Channel.TELEGRAM] = tg
    return Worker(
        repo,
        adapters,
        limiter,
        WorkerConfig(worker_id=worker_id, **({"batch_size": 20, "broadcast_enabled": True} | cfg)),
        clock=clock,
        sleep=sleep or clock.sleep,
    )


# ------------------------------------------------------------------- certificates


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)


@dataclass
class Certs:
    dir: Path
    paths: dict[str, Path] = field(default_factory=dict)

    def __getitem__(self, k: str) -> Path:
        return self.paths[k]


def make_certs(tmp: Path) -> Certs:
    """Fake 8028 CA; pinned 8028 client cert (CA-issued); an unpinned cert from the same
    CA; a self-signed rogue cert; a local server cert for 127.0.0.1/localhost."""
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
        san: list[x509.GeneralName] | None = None,
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
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        c = b.sign(issuer_key or key, hashes.SHA256())
        out.paths[f"{name}_cert"] = tmp / f"{name}.crt"
        out.paths[f"{name}_cert"].write_bytes(c.public_bytes(serialization.Encoding.PEM))
        out.paths[f"{name}_key"] = tmp / f"{name}.key"
        _write_key(out.paths[f"{name}_key"], key)
        return c

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = cert("ca", "fake-8028-ca", ca_key, None, None, ca=True)
    cert(
        "pinned",
        "fake-8028-protean-client",
        ec.generate_private_key(ec.SECP256R1()),
        ca.subject,
        ca_key,
    )
    cert(
        "previous",
        "fake-8028-previous-client",
        ec.generate_private_key(ec.SECP256R1()),
        ca.subject,
        ca_key,
    )
    cert(
        "unpinned",
        "fake-8028-other-client",
        ec.generate_private_key(ec.SECP256R1()),
        ca.subject,
        ca_key,
    )
    cert("rogue", "fake-8028-protean-client", ec.generate_private_key(ec.SECP256R1()), None, None)
    import ipaddress

    cert(
        "server",
        "localhost",
        ec.generate_private_key(ec.SECP256R1()),
        ca.subject,
        ca_key,
        san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
    )
    return out
