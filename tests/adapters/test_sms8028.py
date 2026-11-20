"""MSG-03 8028 SMS adapter tests. Every gateway call is mocked (httpx.MockTransport);
real sockets are blocked for the module. Secrets, certificates and numbers are fake;
certificates are generated per test in a temp directory."""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from oan_messaging.adapters.sms8028 import (
    DEFAULT_BASE_URL,
    ENV_PREFIX,
    Sms8028Adapter,
    Sms8028ConfigError,
    Sms8028Settings,
    build_ssl_context,
    eat_pushdatetime,
    make_client,
    to_domestic,
)
from oan_messaging.contracts import (
    OUTCOME_TO_STATUS,
    Channel,
    FailureCode,
    MessageAdapter,
    MessageStatus,
    Provider,
    ProviderOutcome,
    ProviderResult,
    SmsSendRequest,
    TelegramSendRequest,
)

SECRET = "fake-8028-client-secret-ZXC123"  # noqa: S105 (fake test secret)
NOW = datetime(2026, 9, 1, 5, 12, 13, 987654, tzinfo=UTC)  # 08:12:13.987654 EAT
E164 = "+251900000001"
DOMESTIC = "0900000001"
ENDPOINT = "https://8028.et:3400/sms-api/v1/sms/send-sms"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------- certificates


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _strict(
    b: x509.CertificateBuilder,
    key: ec.EllipticCurvePrivateKey,
    signer: ec.EllipticCurvePrivateKey,
    *,
    ca: bool = False,
) -> x509.CertificateBuilder:
    """Key identifiers (and CA key usage) so Python 3.13+ strict verification accepts them."""
    b = b.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    b = b.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False
    )
    if ca:
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
    return b


@pytest.fixture
def certs(tmp_path: Path) -> dict[str, Path]:
    """Fake CA, client certificate signed by it, matching key (0600) and a foreign key."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _strict(
        x509.CertificateBuilder()
        .subject_name(_name("fake-8028-ca"))
        .issuer_name(_name("fake-8028-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True),
        ca_key,
        ca_key,
        ca=True,
    ).sign(ca_key, hashes.SHA256())
    client_key = ec.generate_private_key(ec.SECP256R1())
    client = _strict(
        x509.CertificateBuilder()
        .subject_name(_name("fake-protean-client"))
        .issuer_name(ca.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30)),
        client_key,
        ca_key,
    ).sign(ca_key, hashes.SHA256())
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = _strict(
        x509.CertificateBuilder()
        .subject_name(_name("8028.et"))
        .issuer_name(ca.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("8028.et")]), critical=False),
        server_key,
        ca_key,
    ).sign(ca_key, hashes.SHA256())
    rogue_key = ec.generate_private_key(ec.SECP256R1())
    rogue = _strict(
        x509.CertificateBuilder()
        .subject_name(_name("8028.et"))
        .issuer_name(_name("8028.et"))
        .public_key(rogue_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("8028.et")]), critical=False),
        rogue_key,
        rogue_key,
    ).sign(rogue_key, hashes.SHA256())
    paths = {
        "ca": tmp_path / "ca.crt",
        "cert": tmp_path / "client.crt",
        "key": tmp_path / "client.key",
        "other_key": tmp_path / "other.key",
        "server_cert": tmp_path / "server.crt",
        "server_key": tmp_path / "server.key",
        "rogue_cert": tmp_path / "rogue.crt",
        "rogue_key": tmp_path / "rogue.key",
    }
    paths["server_cert"].write_bytes(server.public_bytes(serialization.Encoding.PEM))
    paths["server_key"].write_bytes(_pem_key(server_key))
    paths["rogue_cert"].write_bytes(rogue.public_bytes(serialization.Encoding.PEM))
    paths["rogue_key"].write_bytes(_pem_key(rogue_key))
    paths["ca"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths["cert"].write_bytes(client.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(_pem_key(client_key))
    paths["other_key"].write_bytes(_pem_key(ec.generate_private_key(ec.SECP256R1())))
    for k in ("key", "other_key"):
        os.chmod(paths[k], 0o600)
    return paths


def settings(certs: dict[str, Path] | None = None, **kw: Any) -> Sms8028Settings:
    base: dict[str, Any] = {
        "client_secret": SecretStr(SECRET),
        "client_cert": certs["cert"] if certs else Path("client.crt"),
        "client_key": certs["key"] if certs else Path("client.key"),
    }
    base.update(kw)
    return Sms8028Settings(**base)


# ------------------------------------------------------------------------ mocking


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def adapter_for(
    handler: Handler, *, follow_redirects: bool = False, clock: Callable[[], datetime] = lambda: NOW
) -> tuple[Sms8028Adapter, Recorder]:
    rec = Recorder(handler)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(rec), follow_redirects=follow_redirects
    )
    return Sms8028Adapter(settings(), client, clock=clock), rec


def req(text: str = "Your OAN code is 482913.", recipient: str = E164, **kw: Any) -> SmsSendRequest:
    return SmsSendRequest(message_id=uuid.uuid4(), attempt=1, recipient=recipient, text=text, **kw)


def ok_body(sms_id: Any = "17006100", phone: Any = DOMESTIC, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "success": True,
        "message": "SMS records queued successfully.",
        "data": [{"sms_id": sms_id, "phone_number": phone}],
    }
    body.update(extra)
    return body


def respond(
    status: int,
    body: Any = None,
    *,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Handler:
    def handler(_r: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status, content=raw, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return handler


def raising(exc: BaseException) -> Handler:
    def handler(_r: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def assert_no_leak(result: ProviderResult, *needles: str) -> None:
    dumped = result.model_dump_json()
    for needle in (SECRET, DOMESTIC, E164, "251900000001", *needles):
        assert needle not in dumped


# --------------------------------------------------------------------- contract fit


def test_implements_frozen_adapter_protocol() -> None:
    adapter, _ = adapter_for(respond(200, ok_body()))
    assert isinstance(adapter, MessageAdapter)
    assert adapter.provider is Provider.SMS_8028 and adapter.channel is Channel.SMS


async def test_success_request_shape_and_preserved_sms_id() -> None:
    adapter, rec = adapter_for(respond(200, ok_body(sms_id="17006100")))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.SENT
    assert result.provider_message_id == "17006100"
    assert result.provider_status == "http_200" and result.failure_code is None
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.SENT  # queued, not delivered
    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent.method == "POST" and str(sent.url) == ENDPOINT
    assert sent.headers["content-type"] == "application/json; charset=utf-8"
    assert json.loads(sent.content) == {
        "clientSecret": SECRET,
        "smstext": "Your OAN code is 482913.",
        "phonenums": [DOMESTIC],
        "pushdatetime": "2026-09-01 08:12:13",
    }
    assert_no_leak(result, "482913")


async def test_amharic_is_sent_as_raw_utf8_not_escaped() -> None:
    text = "እንደምን አደርሩ! የእርሻ ምክር ለማግኘት 8028 ይደውሉ።"
    adapter, rec = adapter_for(respond(200, ok_body()))
    assert (await adapter.send(req(text=text))).outcome is ProviderOutcome.SENT
    raw = rec.requests[0].content
    assert text.encode("utf-8") in raw and b"\\u" not in raw
    assert json.loads(raw.decode("utf-8"))["smstext"] == text


@pytest.mark.parametrize("sms_id", ["17006100", "A-1.b:2", 17006100])
async def test_sms_id_forms_preserved_as_string(sms_id: Any) -> None:
    adapter, _ = adapter_for(respond(200, ok_body(sms_id=sms_id)))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.SENT and result.provider_message_id == str(sms_id)


async def test_returned_phone_in_other_canonical_forms_still_matches() -> None:
    for phone in ("0900000001", "+251900000001", "251900000001"):
        adapter, _ = adapter_for(respond(200, ok_body(phone=phone)))
        assert (await adapter.send(req())).outcome is ProviderOutcome.SENT


# ----------------------------------------------------------------- phone conversion


@pytest.mark.parametrize(
    ("e164", "domestic"),
    [
        ("+251900000001", "0900000001"),
        ("+251912345678", "0912345678"),
        ("+251712345678", "0712345678"),
        ("+251799999999", "0799999999"),
    ],
)
def test_to_domestic(e164: str, domestic: str) -> None:
    assert to_domestic(e164) == domestic


@pytest.mark.parametrize(
    "bad",
    [
        "0912345678",
        "251912345678",
        "+251812345678",
        "+25191234567",
        "+2519123456789",
        "+1 202 555 0100",
        "",
    ],
)
def test_to_domestic_rejects_everything_else(bad: str) -> None:
    with pytest.raises(ValueError) as info:
        to_domestic(bad)
    if bad:
        assert bad not in str(info.value)


async def test_invalid_recipient_rejected_without_a_call() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    bad = SmsSendRequest.model_construct(
        message_id=uuid.uuid4(),
        attempt=1,
        recipient="+251812345678",
        text="hi",
        channel=Channel.SMS,
        expires_at=None,
    )
    result = await adapter.send(bad)
    assert result.outcome is ProviderOutcome.REJECTED
    assert result.failure_code is FailureCode.INVALID_RECIPIENT
    assert rec.requests == [] and "812345678" not in result.model_dump_json()


# ------------------------------------------------------------------ time conversion


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 1, 5, 12, 13, 987654, tzinfo=UTC), "2026-09-01 08:12:13"),  # truncated
        (datetime(2026, 9, 1, 21, 30, 0, tzinfo=UTC), "2026-09-02 00:30:00"),  # day rollover
        (datetime(2026, 12, 31, 22, 0, 0, tzinfo=UTC), "2027-01-01 01:00:00"),  # year rollover
        (datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC), "2026-03-01 02:59:59"),
        (
            datetime(2026, 9, 1, 8, 12, 13, tzinfo=timezone(timedelta(hours=3))),
            "2026-09-01 08:12:13",
        ),
        (
            datetime(2026, 9, 1, 1, 12, 13, tzinfo=timezone(timedelta(hours=-4))),
            "2026-09-01 08:12:13",
        ),
        (datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC), "2026-07-01 15:00:00"),  # no DST in EAT
    ],
)
def test_eat_pushdatetime(moment: datetime, expected: str) -> None:
    assert eat_pushdatetime(moment) == expected


def test_eat_pushdatetime_rejects_naive() -> None:
    with pytest.raises(ValueError):
        eat_pushdatetime(datetime(2026, 9, 1, 5, 12, 13))


async def test_pushdatetime_is_now_from_the_clock() -> None:
    adapter, rec = adapter_for(
        respond(200, ok_body()), clock=lambda: datetime(2026, 9, 1, 22, 0, 1, tzinfo=UTC)
    )
    await adapter.send(req())
    assert json.loads(rec.requests[0].content)["pushdatetime"] == "2026-09-02 01:00:01"


async def test_naive_clock_is_a_config_error_without_a_call() -> None:
    adapter, rec = adapter_for(
        respond(200, ok_body()), clock=lambda: datetime(2026, 9, 1, 5, 12, 13)
    )
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.NOT_SENT and not result.retryable
    assert result.failure_code is FailureCode.PROVIDER_CONFIG and rec.requests == []


async def test_expired_request_not_sent() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(expires_at=NOW - timedelta(seconds=1)))
    assert result.outcome is ProviderOutcome.NOT_SENT and not result.retryable
    assert result.failure_code is FailureCode.EXPIRED_BEFORE_SEND and rec.requests == []


# ------------------------------------------------------ English/Amharic boundaries

EMOJI = "😀"


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("a" * 160, True),
        ("a" * 459, True),  # 3 parts x 153 septets
        ("a" * 460, False),
        ("{" * 80, True),
        ("{" * 228 + "a", True),  # GSM-7 extension chars count 2 and are never split
        ("{" * 228 + "aa", False),
        ("€" * 228, True),
        ("ሀ" * 70, True),
        ("ሀ" * 201, True),  # any Amharic -> UCS-2, 3 parts x 67 units
        ("ሀ" * 202, False),
        ("a" * 200 + "ሀ", True),  # one Amharic char makes it Unicode
        ("a" * 201 + "ሀ", False),
        (EMOJI * 99, True),  # emoji are 2 UTF-16 units, kept whole: 33 per part
        (EMOJI * 100, False),
        ("Line one\nline two", True),
    ],
)
async def test_part_limit_boundaries(text: str, accepted: bool) -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(text=text))
    if accepted:
        assert result.outcome is ProviderOutcome.SENT
        assert json.loads(rec.requests[0].content)["smstext"] == text
    else:
        assert result.outcome is ProviderOutcome.REJECTED
        assert result.failure_code is FailureCode.TEXT_REJECTED and rec.requests == []


@pytest.mark.parametrize("text", ["", "   ", "bell\x07", "nul\x00"])
async def test_blank_or_control_text_rejected_without_a_call(text: str) -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(text=text))
    assert result.outcome is ProviderOutcome.REJECTED and rec.requests == []


async def test_wrong_channel_request_refused_without_a_call() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    tg = TelegramSendRequest(message_id=uuid.uuid4(), attempt=1, bot="b", chat_id=5, text="x")
    result = await adapter.send(tg)
    assert result.outcome is ProviderOutcome.NOT_SENT
    assert result.failure_code is FailureCode.PROVIDER_CONFIG and rec.requests == []


# ------------------------------------------------- malformed and partial success


@pytest.mark.parametrize(
    "handler",
    [
        respond(200, raw=b"<html>proxy</html>"),
        respond(200, raw=b""),
        respond(200, raw=b"\xff\xfe"),
        respond(200, [ok_body()]),
        respond(200, {"message": "queued", "data": ok_body()["data"]}),  # success missing
        respond(200, ok_body(success="true")),
        respond(200, ok_body(success=1)),
        respond(200, ok_body(success=False)),
        respond(200, {"success": True}),  # data missing
        respond(200, {"success": True, "data": []}),
        respond(200, {"success": True, "data": {"sms_id": "1"}}),
        respond(200, {"success": True, "data": ["17006100"]}),
        respond(
            200,
            {
                "success": True,
                "data": [  # partial / multi-recipient
                    {"sms_id": "1", "phone_number": DOMESTIC},
                    {"sms_id": "2", "phone_number": "0900000002"},
                ],
            },
        ),
        respond(200, ok_body(phone="0900000002")),  # different recipient
        respond(200, ok_body(phone="091261825")),
        respond(200, ok_body(phone=None)),
        respond(200, ok_body(sms_id=None)),
        respond(200, ok_body(sms_id="")),
        respond(200, ok_body(sms_id=" 17006100")),
        respond(200, ok_body(sms_id="x" * 65)),
        respond(200, ok_body(sms_id=True)),
        respond(200, ok_body(sms_id=0)),
        respond(200, ok_body(sms_id=[1])),
        respond(201, ok_body(success=False)),
    ],
    ids=[
        "html",
        "empty",
        "not-utf8",
        "array",
        "no-success",
        "success-str",
        "success-int",
        "success-false",
        "no-data",
        "empty-data",
        "data-object",
        "data-scalar",
        "partial-two-entries",
        "other-recipient",
        "invalid-phone",
        "no-phone",
        "no-id",
        "empty-id",
        "space-id",
        "long-id",
        "bool-id",
        "zero-id",
        "list-id",
        "201-success-false",
    ],
)
async def test_malformed_or_partial_success_is_unknown(handler: Handler) -> None:
    adapter, rec = adapter_for(handler)
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert result.failure_code is FailureCode.MALFORMED_RESPONSE
    assert result.provider_message_id is None
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.UNKNOWN
    assert len(rec.requests) == 1
    assert_no_leak(result)


# ------------------------------------------------ definite rejections vs ambiguous


@pytest.mark.parametrize(
    ("status", "body", "outcome", "code", "retryable"),
    [
        (
            400,
            {"success": False, "message": "Invalid phone number: 091261825"},
            ProviderOutcome.REJECTED,
            FailureCode.INVALID_RECIPIENT,
            False,
        ),
        (
            400,
            {"success": False, "message": "smstext is required"},
            ProviderOutcome.REJECTED,
            FailureCode.TEXT_REJECTED,
            False,
        ),
        (
            400,
            {"success": False, "message": "Wrong date format"},
            ProviderOutcome.REJECTED,
            FailureCode.PROVIDER_REJECTED,
            False,
        ),
        (400, None, ProviderOutcome.REJECTED, FailureCode.PROVIDER_REJECTED, False),
        (
            401,
            {"success": False, "message": "Invalid client secret"},
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_AUTH,
            False,
        ),
        (
            403,
            {"success": False, "message": "Credentials suspended"},
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_AUTH,
            False,
        ),
        (
            413,
            {"success": False, "message": "Payload too large"},
            ProviderOutcome.REJECTED,
            FailureCode.TEXT_REJECTED,
            False,
        ),
        (404, None, ProviderOutcome.REJECTED, FailureCode.PROVIDER_REJECTED, False),
        (429, None, ProviderOutcome.UNKNOWN, FailureCode.RATE_LIMITED, False),
        (  # 8028, 20 Nov 2026: a 500 queues nothing, so it is retried
            500,
            {"success": False},
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_SERVER_ERROR,
            True,
        ),
        (502, None, ProviderOutcome.UNKNOWN, FailureCode.PROVIDER_SERVER_ERROR, False),
        (
            503,
            {"success": False, "message": "Temporary error"},
            ProviderOutcome.UNKNOWN,
            FailureCode.PROVIDER_SERVER_ERROR,
            False,
        ),
        (504, None, ProviderOutcome.UNKNOWN, FailureCode.PROVIDER_SERVER_ERROR, False),
        (400, ok_body(), ProviderOutcome.UNKNOWN, FailureCode.MALFORMED_RESPONSE, False),
        (302, None, ProviderOutcome.UNKNOWN, FailureCode.MALFORMED_RESPONSE, False),
    ],
    ids=[
        "400-phone",
        "400-text",
        "400-date",
        "400-nonjson",
        "401",
        "403",
        "413",
        "404",
        "429",
        "500",
        "502",
        "503",
        "504",
        "400-with-success-true",
        "302",
    ],
)
async def test_status_classification_preserves_gateway_status(
    status: int, body: Any, outcome: ProviderOutcome, code: FailureCode, retryable: bool
) -> None:
    handler = respond(status, raw=b"<html/>") if body is None else respond(status, body)
    adapter, rec = adapter_for(handler)
    result = await adapter.send(req())
    assert (result.outcome, result.failure_code, result.retryable) == (outcome, code, retryable)
    assert result.provider_status == f"http_{status}"
    assert len(rec.requests) == 1  # the adapter itself never resends; the worker decides
    assert_no_leak(result, "091261825", "Invalid client secret", "Temporary error")


@pytest.mark.parametrize(
    "handler",
    [
        respond(429, {"success": False, "message": "Too many requests"}),
        respond(429, {"success": False}),
        respond(429, raw=b"<html>rate limited</html>"),
        respond(429, raw=b""),
        respond(429, {"success": False}, headers={"Retry-After": "17"}),
        respond(429, {"success": False}, headers={"Retry-After": "0"}),
        respond(429, {"success": False}, headers={"Retry-After": "Wed, 01 Sep 2026 05:13:13 GMT"}),
        respond(429, raw=b"", headers={"Retry-After": "soon"}),
        respond(429, raw=b"", headers={"Retry-After": "-5"}),
    ],
    ids=[
        "success-false-msg",
        "success-false",
        "html",
        "empty",
        "retry-after-17",
        "retry-after-0",
        "retry-after-date",
        "retry-after-malformed",
        "retry-after-negative",
    ],
)
async def test_429_is_unknown_rate_limited_never_retried(handler: Handler) -> None:
    """8028 does not document 429 or confirm it means 'not queued': unknown, no retry."""
    adapter, rec = adapter_for(handler)
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.failure_code is FailureCode.RATE_LIMITED
    assert result.retryable is False and result.retry_after_seconds is None
    assert result.provider_status == "http_429" and result.provider_message_id is None
    assert result.detail is not None and "unconfirmed" in result.detail
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.UNKNOWN
    assert len(rec.requests) == 1  # one request, no internal retry
    assert_no_leak(result, "Too many requests")


async def test_429_with_contradictory_success_true_stays_malformed_unknown() -> None:
    adapter, rec = adapter_for(respond(429, ok_body()))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert result.failure_code is FailureCode.MALFORMED_RESPONSE
    assert result.provider_message_id is None and len(rec.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_never_followed_even_if_client_allows(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "8028.et":
            return httpx.Response(status, headers={"Location": "https://evil.invalid/collect"})
        return httpx.Response(200, json=ok_body())

    adapter, rec = adapter_for(handler, follow_redirects=True)
    result = await adapter.send(req())
    assert [r.url.host for r in rec.requests] == ["8028.et"]
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert_no_leak(result, "evil.invalid")


def _ssl_cause(exc: httpx.ConnectError) -> httpx.ConnectError:
    exc.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    return exc


def _reset_cause(exc: httpx.ConnectError) -> httpx.ConnectError:
    exc.__cause__ = ConnectionResetError("reset during handshake")
    return exc


@pytest.mark.parametrize(
    ("exc", "outcome", "code", "retryable"),
    [
        (
            httpx.ConnectError("refused"),
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_UNAVAILABLE,
            True,
        ),
        (
            httpx.ConnectTimeout("connect"),
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_UNAVAILABLE,
            True,
        ),
        (
            httpx.PoolTimeout("pool"),
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_UNAVAILABLE,
            True,
        ),
        (
            _ssl_cause(httpx.ConnectError("tls")),
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_CONFIG,
            False,
        ),
        (
            _reset_cause(httpx.ConnectError("reset")),
            ProviderOutcome.NOT_SENT,
            FailureCode.PROVIDER_CONFIG,
            False,
        ),
        (httpx.ReadTimeout("read"), ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT, False),
        (httpx.WriteTimeout("write"), ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT, False),
        (
            httpx.RemoteProtocolError("disconnected"),
            ProviderOutcome.UNKNOWN,
            FailureCode.MALFORMED_RESPONSE,
            False,
        ),
        (
            httpx.ReadError("reset after send"),
            ProviderOutcome.UNKNOWN,
            FailureCode.MALFORMED_RESPONSE,
            False,
        ),
        (
            httpx.WriteError("broken pipe"),
            ProviderOutcome.UNKNOWN,
            FailureCode.MALFORMED_RESPONSE,
            False,
        ),
    ],
    ids=[
        "refused",
        "connect-timeout",
        "pool-timeout",
        "tls",
        "handshake-reset",
        "read-timeout",
        "write-timeout",
        "disconnect",
        "read-error",
        "write-error",
    ],
)
async def test_transport_failures(
    exc: BaseException, outcome: ProviderOutcome, code: FailureCode, retryable: bool
) -> None:
    adapter, rec = adapter_for(raising(exc))
    result = await adapter.send(req())
    assert (result.outcome, result.failure_code, result.retryable) == (outcome, code, retryable)
    assert len(rec.requests) == 1  # one attempt, no internal retry
    assert_no_leak(result)


async def test_unexpected_exception_is_unknown_and_does_not_leak() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"exploded with body {request.content!r}")

    adapter, _ = adapter_for(boom)
    result = await adapter.send(req())  # never raises
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert result.failure_code is FailureCode.INTERRUPTED
    assert result.detail == "unexpected RuntimeError"
    assert_no_leak(result, "482913")


# ------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "handler",
    [
        respond(200, ok_body()),
        respond(400, {"success": False, "message": "Invalid phone number: 0900000001"}),
        respond(401, {"success": False}),
        respond(503, raw=b"down"),
        raising(httpx.ReadTimeout("t")),
        raising(RuntimeError(f"clientSecret={SECRET} phonenums={DOMESTIC}")),
    ],
    ids=["ok", "400", "401", "503", "timeout", "unexpected"],
)
async def test_logs_never_contain_secret_body_text_or_phone(
    handler: Handler, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter, _ = adapter_for(handler)
    result = await adapter.send(req(text="private body 482913"))
    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    for needle in (SECRET, "private body 482913", DOMESTIC, E164, "251900000001"):
        assert needle not in logged
    assert any(r.name == "oan_messaging.adapters.sms8028" for r in caplog.records)
    assert_no_leak(result, "private body 482913")


def test_secret_redacting_filter_scrubs_third_party_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter_for(respond(200, ok_body()))  # installs filters
    logging.getLogger("httpx").debug("leaky library line %s", f'{{"clientSecret":"{SECRET}"}}')
    assert SECRET not in caplog.text and "<redacted>" in caplog.text


def test_reprs_hide_the_secret() -> None:
    s = settings()
    adapter, _ = adapter_for(respond(200, ok_body()))
    for shown in (repr(s), str(s), repr(adapter), repr(s.client_secret)):
        assert SECRET not in shown


# ------------------------------------------------------------- TLS configuration


def test_ssl_context_is_mtls_with_verification(certs: dict[str, Path]) -> None:
    ctx = build_ssl_context(settings(certs))
    assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def common_names(cert: Any) -> list[str]:
    return [value for rdn in cert["subject"] for key, value in rdn if key == "commonName"]


def fake_gateway_context(certs: dict[str, Path], *, rogue: bool = False) -> ssl.SSLContext:
    """A stand-in 8028 server that requires a client certificate from the fake CA."""
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    name = "rogue" if rogue else "server"
    sctx.load_cert_chain(str(certs[f"{name}_cert"]), str(certs[f"{name}_key"]))
    sctx.verify_mode = ssl.CERT_REQUIRED
    sctx.load_verify_locations(cafile=str(certs["ca"]))
    return sctx


def memory_handshake(cctx: ssl.SSLContext, sctx: ssl.SSLContext, hostname: str) -> dict[str, Any]:
    """Complete a TLS handshake over in-memory BIOs (no sockets). Returns the client
    certificate the server saw."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = cctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=hostname)
    server = sctx.wrap_bio(s_in, s_out, server_side=True)
    done = {"c": False, "s": False}
    for _ in range(50):
        for key, side in (("c", client), ("s", server)):
            if not done[key]:
                try:
                    side.do_handshake()
                    done[key] = True
                except ssl.SSLWantReadError:
                    pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if all(done.values()):
            # TLS 1.3 client-certificate checks finish on the first application data.
            client.write(b"ping")
            s_in.write(c_out.read())
            assert server.read(4) == b"ping"
            peer = server.getpeercert()
            assert peer is not None
            return peer
    raise AssertionError("handshake did not complete")


def test_mtls_handshake_presents_client_certificate(certs: dict[str, Path]) -> None:
    cctx = build_ssl_context(settings(certs, ca_cert=certs["ca"]))
    peer = memory_handshake(cctx, fake_gateway_context(certs), "8028.et")
    assert common_names(peer) == ["fake-protean-client"]


def test_wrong_hostname_is_refused(certs: dict[str, Path]) -> None:
    cctx = build_ssl_context(settings(certs, ca_cert=certs["ca"]))
    with pytest.raises(ssl.SSLCertVerificationError):
        memory_handshake(cctx, fake_gateway_context(certs), "196.188.0.1")


def test_untrusted_server_certificate_is_refused(certs: dict[str, Path]) -> None:
    cctx = build_ssl_context(settings(certs, ca_cert=certs["ca"]))
    with pytest.raises(ssl.SSLCertVerificationError):
        memory_handshake(cctx, fake_gateway_context(certs, rogue=True), "8028.et")


def test_server_without_8028_ca_is_not_trusted_by_default(certs: dict[str, Path]) -> None:
    # Without the 8028 ca.crt only system CAs are trusted: the fake CA is not.
    cctx = build_ssl_context(settings(certs))
    with pytest.raises(ssl.SSLCertVerificationError):
        memory_handshake(cctx, fake_gateway_context(certs), "8028.et")


def test_ssl_context_with_8028_ca_file(certs: dict[str, Path]) -> None:
    ctx = build_ssl_context(settings(certs, ca_cert=certs["ca"]))
    assert ["fake-8028-ca"] in [common_names(c) for c in ctx.get_ca_certs()]


def test_mismatched_key_and_cert_refused_without_detail(certs: dict[str, Path]) -> None:
    with pytest.raises(Sms8028ConfigError) as info:
        build_ssl_context(settings(certs, client_key=certs["other_key"]))
    assert "BEGIN" not in str(info.value) and str(certs["other_key"]) not in str(info.value)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660])
def test_private_key_must_be_0600(certs: dict[str, Path], mode: int) -> None:
    os.chmod(certs["key"], mode)
    with pytest.raises(Sms8028ConfigError):
        build_ssl_context(settings(certs))


def test_missing_files_refused(certs: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(Sms8028ConfigError):
        build_ssl_context(settings(certs, client_cert=tmp_path / "nope.crt"))
    with pytest.raises(Sms8028ConfigError):
        build_ssl_context(settings(certs, ca_cert=tmp_path / "nope-ca.crt"))


async def test_make_client_uses_the_mtls_context_and_safe_defaults(
    certs: dict[str, Path],
) -> None:
    client = make_client(settings(certs, ca_cert=certs["ca"]))
    try:
        assert client.follow_redirects is False and client.trust_env is False
        assert client.timeout.connect == 5.0 and client.timeout.read == 15.0
        ctx = client._transport._pool._ssl_context  # type: ignore[attr-defined]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "base",
    [
        "http://8028.et:3400/sms-api/v1",
        "https://u:p@8028.et/x",
        "https://8028.et/x?debug=1",
        "ftp://8028.et",
        "https:///x",
    ],
)
def test_base_url_must_be_https_without_credentials(base: str) -> None:
    with pytest.raises(Sms8028ConfigError):
        settings(base_url=base)


@pytest.mark.parametrize("secret", ["", " padded ", "trailing\n"])
def test_secret_must_be_clean(secret: str) -> None:
    with pytest.raises(Sms8028ConfigError) as info:
        settings(client_secret=SecretStr(secret))
    if secret.strip():
        assert secret.strip() not in str(info.value)


def test_settings_from_environment(certs: dict[str, Path]) -> None:
    env = {
        f"{ENV_PREFIX}CLIENT_SECRET": SECRET,
        f"{ENV_PREFIX}CLIENT_CERT": str(certs["cert"]),
        f"{ENV_PREFIX}CLIENT_KEY": str(certs["key"]),
        f"{ENV_PREFIX}CA_CERT": str(certs["ca"]),
    }
    s = Sms8028Settings.from_environ(env)
    assert s.endpoint == DEFAULT_BASE_URL + "/sms/send-sms" and s.ca_cert == certs["ca"]
    assert s.client_secret.get_secret_value() == SECRET
    with pytest.raises(Sms8028ConfigError) as info:
        Sms8028Settings.from_environ({f"{ENV_PREFIX}CLIENT_SECRET": SECRET})
    assert SECRET not in str(info.value)


def test_handshake_without_client_certificate_fails(certs: dict[str, Path]) -> None:
    # Control: the same fake gateway rejects a client that has no 8028 certificate, so the
    # positive handshake test above really proves the adapter's context presents one.
    plain = ssl.create_default_context(cafile=str(certs["ca"]))
    with pytest.raises(ssl.SSLError):
        memory_handshake(plain, fake_gateway_context(certs), "8028.et")
