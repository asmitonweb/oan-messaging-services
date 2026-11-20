"""8028 receipt ingress: verified mTLS (real TLS over loopback), strict parsing, persist
before acknowledgement, duplicates/out-of-order/early/undocumented statuses, recipient
matching after purge and key rotation. Real PostgreSQL; fake clock; fake providers."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from wsupport import (
    T0,
    Certs,
    FakeClock,
    FakeSms,
    FakeTelegram,
    Keys,
    make_caller,
    make_certs,
    make_worker,
    sent,
    submit_broadcast,
    submit_telegram,
)

from oan_messaging.contracts import Provider
from oan_messaging.receipts import (
    DEFAULT_PATH,
    CallbackServer,
    ReceiptProcessor,
    ReceiptRejected,
    build_server_ssl_context,
    load_pins,
    parse_callback,
)
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import messages, receipts

PHONE = "0900000001"
CB = DEFAULT_PATH  # the callback path registered with 8028


@pytest.fixture
def certs(tmp_path: Path) -> Certs:
    return make_certs(tmp_path)


def der(path: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(path.read_text())


def body(
    sms_id: Any = "17006100",
    phone: Any = PHONE,
    status: Any = "Delivered",
    event_time: Any = "2026-09-01 08:12:19",
    **extra: Any,
) -> bytes:
    data = {
        "sms_id": sms_id,
        "phone_number": phone,
        "status": status,
        "event_time": event_time,
        **extra,
    }
    return json.dumps({k: v for k, v in data.items() if v is not None}).encode()


JSON = {"content-type": "application/json"}


async def stored_receipts(sf: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    async with sf() as s:
        return [
            dict(r)
            for r in (await s.execute(select(receipts).order_by(receipts.c.received_at))).mappings()
        ]


async def status_of(sf: async_sessionmaker[AsyncSession], mid: uuid.UUID) -> dict[str, Any]:
    async with sf() as s:
        return dict(
            (await s.execute(select(messages).where(messages.c.id == mid))).mappings().one()
        )


async def sent_sms(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    pid: str = "17006100",
) -> uuid.UUID:
    ctx = await make_caller(sf, keys, f"svc-{pid}")
    m = await submit_broadcast(repo, ctx, 1)
    sms = FakeSms(clock, lambda req, now: sent(Provider.SMS_8028, now, pid))
    assert (await make_worker(repo, engine, clock, sms=sms).run_once()).outcomes == {"sent": 1}
    return m.id


# --------------------------------------------------------------------- parsing


def test_parse_callback_converts_eat_and_normalizes() -> None:
    now = T0
    r = parse_callback(body(sms_id=17006100), now)
    assert r.provider_message_id == "17006100" and r.recipient == "+251900000001"
    assert r.raw_status == "Delivered" and r.received_at == now
    assert r.event_time == datetime(2026, 9, 1, 5, 12, 19, tzinfo=UTC)
    assert parse_callback(body(phone="+251900000001"), now).recipient == "+251900000001"
    assert parse_callback(body(status="SomethingNew"), now).raw_status == "SomethingNew"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not json", "invalid_json"),
        (b"[1]", "invalid_json"),
        (body(sms_id=None), "invalid_sms_id"),
        (body(sms_id=True), "invalid_sms_id"),
        (body(sms_id=" 1"), "invalid_sms_id"),
        (body(sms_id="x" * 65), "invalid_sms_id"),
        (body(phone="0111000000"), "invalid_phone_number"),
        (body(phone=900000001), "invalid_phone_number"),
        (body(status="x" * 65), "invalid_status"),
        (body(status="Deliv\nered"), "invalid_status"),
        (body(event_time="2026-09-01T08:12:19"), "invalid_event_time"),
        (body(event_time="2026-02-30 08:12:19"), "invalid_event_time"),
        (body(event_time=None), "invalid_event_time"),
        (b"{" + b" " * 5000 + b"}", "payload_too_large"),
    ],
)
def test_parse_callback_rejects_without_echo(payload: bytes, code: str) -> None:
    with pytest.raises(ReceiptRejected) as info:
        parse_callback(payload, T0)
    assert info.value.code == code and "0111000000" not in str(info.value)


# ---------------------------------------------------- processor (TLS-agnostic)


async def test_processor_requires_pinned_peer_certificate_and_ignores_headers(
    sf: async_sessionmaker[AsyncSession],
    repo: PostgresMessageRepository,
    certs: Certs,
) -> None:
    pins = load_pins([certs["pinned_cert"]])
    proc = ReceiptProcessor(repo, pins, clock=lambda: T0)
    pinned_pem = certs["pinned_cert"].read_text()
    spoof = {
        **JSON,
        "x-ssl-client-cert": pinned_pem.replace("\n", ""),
        "x-client-cert-fingerprint": pins.__iter__().__next__().hex(),
        "x-forwarded-client-cert": "Hash=whatever",
        "ssl-client-verify": "SUCCESS",
    }
    for peer in (None, b"", der(certs["unpinned_cert"]), der(certs["rogue_cert"])):
        r = await proc.handle(peer, "POST", CB, spoof, body())
        assert r.status == 403
    assert await stored_receipts(sf) == []  # nothing persisted for unauthenticated calls
    ok = await proc.handle(der(certs["pinned_cert"]), "POST", CB, JSON, body())
    assert ok.status == 200 and len(await stored_receipts(sf)) == 1


async def test_processor_http_errors(
    repo: PostgresMessageRepository, certs: Certs, sf: async_sessionmaker[AsyncSession]
) -> None:
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=lambda: T0)
    peer = der(certs["pinned_cert"])
    assert (await proc.handle(peer, "GET", CB, JSON, b"")).status == 405
    assert (await proc.handle(peer, "POST", "/other", JSON, body())).status == 404
    assert (
        await proc.handle(peer, "POST", CB, {"content-type": "text/plain"}, body())
    ).status == 415
    bad = await proc.handle(peer, "POST", CB, JSON, body(phone="0111000000"))
    assert bad.status == 400 and b"0111000000" not in bad.body
    assert await stored_receipts(sf) == []


async def test_not_acknowledged_unless_persisted(
    repo: PostgresMessageRepository,
    certs: Certs,
    sf: async_sessionmaker[AsyncSession],
) -> None:
    class Down:
        async def record_receipt(self, *a: Any) -> Any:
            raise ConnectionError("database unavailable")

    proc = ReceiptProcessor(
        cast(PostgresMessageRepository, Down()),
        load_pins([certs["pinned_cert"]]),
        clock=lambda: T0,
    )
    r = await proc.handle(der(certs["pinned_cert"]), "POST", CB, JSON, body())
    assert r.status == 503 and b"retry_later" in r.body  # 8028 retries; no 200 issued
    assert await stored_receipts(sf) == []


# ---------------------------------------------------------- receipt semantics


async def test_duplicate_and_out_of_order_callbacks(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    peer = der(certs["pinned_cert"])
    for st in ("Delivered", "Delivered", "Sent"):  # duplicate, then a late "Sent"
        r = await proc.handle(peer, "POST", CB, JSON, body(status=st))
        assert r.status == 200  # always acknowledged once persisted
    rows = await stored_receipts(sf)
    assert [r["disposition"] for r in rows] == [
        "applied",
        "recorded_no_change",
        "recorded_no_change",
    ]
    m = await status_of(sf, mid)
    assert m["status"] == "delivered" and m["delivered_at"] == datetime(
        2026, 9, 1, 5, 12, 19, tzinfo=UTC
    )


async def test_undocumented_status_is_recorded_without_transition(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    for st in ("Failed", "Queued", "UNKNOWN"):
        r = await proc.handle(
            der(certs["pinned_cert"]), "POST", CB, JSON, body(status=st)
        )
        assert r.status == 200
    assert (await status_of(sf, mid))["status"] == "sent"
    assert [r["raw_status"] for r in await stored_receipts(sf)] == ["Failed", "Queued", "UNKNOWN"]
    exp = await proc.handle(
        der(certs["pinned_cert"]), "POST", CB, JSON, body(status="Expired")
    )
    assert exp.status == 200
    m = await status_of(sf, mid)
    assert (m["status"], m["failure_code"], m["provider_status"]) == (
        "failed",
        "provider_delivery_expired",
        "Expired",
    )


async def test_wrong_recipient_receipt_is_mismatched(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    r = await proc.handle(
        der(certs["pinned_cert"]), "POST", CB, JSON, body(phone="0900000999")
    )
    assert r.status == 200
    assert (await stored_receipts(sf))[0]["disposition"] == "mismatched"
    assert (await status_of(sf, mid))["status"] == "sent"


async def test_early_callback_before_send_response_is_stored_then_applied(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_broadcast(repo, ctx, 1)
    proc = ReceiptProcessor(keys.repo(sf), load_pins([certs["pinned_cert"]]), clock=clock)

    async def gateway_calls_back_before_responding(req: Any, now: Any) -> Any:
        r = await proc.handle(
            der(certs["pinned_cert"]), "POST", CB, JSON, body(sms_id="555")
        )
        assert r.status == 200  # persisted as an orphan, acknowledged
        return sent(Provider.SMS_8028, now, "555")

    w = make_worker(repo, engine, clock, sms=FakeSms(clock, gateway_calls_back_before_responding))
    assert (await w.run_once()).outcomes == {"delivered": 1}
    assert (await status_of(sf, m.id))["status"] == "delivered"
    assert [r["disposition"] for r in await stored_receipts(sf)] == ["applied"]


async def test_receipt_matches_after_content_purge_and_key_rotation(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)  # fingerprints under f1, content purged
    assert (await status_of(sf, mid))["content_purged_at"] is not None
    rotated = keys.rotated_fingerprint("f2")
    proc = ReceiptProcessor(rotated.repo(sf), load_pins([certs["pinned_cert"]]), clock=clock)
    r = await proc.handle(der(certs["pinned_cert"]), "POST", CB, JSON, body())
    assert r.status == 200
    (rec,) = await stored_receipts(sf)
    assert (rec["disposition"], rec["recipient_fp_version"]) == ("applied", "f1")
    assert (await status_of(sf, mid))["status"] == "delivered"


async def test_telegram_has_no_receipts_via_this_ingress(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    ctx = await make_caller(sf, keys)
    m = await submit_telegram(repo, ctx)
    tg = FakeTelegram(clock, lambda req, now: sent(Provider.TELEGRAM, now, "17006100"))
    await make_worker(repo, engine, clock, tg=tg).run_once()
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    r = await proc.handle(der(certs["pinned_cert"]), "POST", CB, JSON, body())
    assert r.status == 200
    assert (await stored_receipts(sf))[0]["disposition"] == "orphaned"  # SMS-only matching
    assert (await status_of(sf, m.id))["status"] == "sent"


async def test_no_raw_phone_or_secrets_in_storage_or_logs(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await sent_sms(engine, sf, keys, repo, clock)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    for payload in (body(), body(phone="0900000777"), body(phone="0111000000")):
        await proc.handle(der(certs["pinned_cert"]), "POST", CB, JSON, payload)
    dump = repr(await stored_receipts(sf))
    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    for raw in (
        "0900000001",
        "+251900000001",
        "900000001",
        "0900000777",
        "900000777",
        "0111000000",
        "Advisory 1",
    ):
        assert raw not in dump and raw not in logged
    assert "+2519******01" in dump


# ------------------------------------------------------ real TLS over loopback


@pytest.fixture
async def server(
    repo: PostgresMessageRepository, certs: Certs, clock: FakeClock
) -> AsyncIterator[int]:
    pins = load_pins([certs["pinned_cert"], certs["previous_cert"]])
    ctx = build_server_ssl_context(
        certs["server_cert"], certs["server_key"], [certs["pinned_cert"], certs["previous_cert"]]
    )
    srv = CallbackServer(ReceiptProcessor(repo, pins, clock=clock), ctx)
    port = await srv.start("127.0.0.1", 0)
    yield port
    await srv.close()


def client_ctx(certs: Certs, who: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(certs["ca_cert"]))
    if who is not None:
        ctx.load_cert_chain(str(certs[f"{who}_cert"]), str(certs[f"{who}_key"]))
    return ctx


async def post(
    port: int, ctx: ssl.SSLContext, payload: bytes, *, headers: str | None = None
) -> tuple[int, bytes]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, ssl=ctx, server_hostname="localhost"), 5
    )
    head = (
        headers
        if headers is not None
        else (
            "POST /sms-status-receiver/v1/update-status HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
        )
    )
    writer.write(head.encode() + payload)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    status_line, _, rest = raw.partition(b"\r\n")
    return int(status_line.split()[1]), rest.split(b"\r\n\r\n", 1)[1]


async def raw_exchange(
    port: int, ctx: ssl.SSLContext, payload: bytes, *, headers: str | None = None
) -> bytes:
    """Send one request; return the raw response bytes, or b"" when the server tore the
    TLS session down (handshake/certificate rejection) without any HTTP response."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, ssl=ctx, server_hostname="localhost"), 5
        )
        head = (
            headers
            if headers is not None
            else (
                "POST /sms-status-receiver/v1/update-status HTTP/1.1\r\nHost: localhost\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
            )
        )
        writer.write(head.encode() + payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return raw
    except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
        return b""


async def test_tls_pinned_client_is_accepted_and_persisted_before_200(
    server: int,
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)
    status, payload = await post(server, client_ctx(certs, "pinned"), body())
    assert status == 200 and json.loads(payload) == {"success": True}
    # By the time the 200 is read, the receipt is durable and applied.
    assert (await stored_receipts(sf))[0]["disposition"] == "applied"
    assert (await status_of(sf, mid))["status"] == "delivered"
    # The previous certificate (renewal overlap) is also pinned.
    status, _ = await post(server, client_ctx(certs, "previous"), body(status="Sent"))
    assert status == 200 and len(await stored_receipts(sf)) == 2


@pytest.mark.parametrize(
    "who",
    [None, "unpinned", "rogue"],
    ids=["no-client-cert", "same-ca-not-pinned", "self-signed-same-cn"],
)
async def test_tls_rejects_unpinned_or_missing_client_certificates(
    server: int,
    sf: async_sessionmaker[AsyncSession],
    certs: Certs,
    who: str | None,
) -> None:
    # Rejected during the TLS handshake: no HTTP response at all (not even a 403).
    assert await raw_exchange(server, client_ctx(certs, who), body()) == b""
    assert await stored_receipts(sf) == []


async def test_tls_header_spoofing_cannot_authenticate(
    server: int,
    sf: async_sessionmaker[AsyncSession],
    certs: Certs,
) -> None:
    payload = body()
    spoof = (
        "POST /sms-status-receiver/v1/update-status HTTP/1.1\r\nHost: localhost\r\n"
        "X-SSL-Client-Cert: " + certs["pinned_cert"].read_text().replace("\n", "") + "\r\n"
        "X-SSL-Client-Verify: SUCCESS\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    assert await raw_exchange(server, client_ctx(certs, "unpinned"), payload, headers=spoof) == b""
    assert await stored_receipts(sf) == []


async def test_plain_http_to_the_tls_port_is_refused(
    server: int,
    sf: async_sessionmaker[AsyncSession],
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server)
    payload = body()
    writer.write(
        b"POST /sms-status-receiver/v1/update-status HTTP/1.1\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    assert not raw.startswith(b"HTTP/1.1 200")
    assert await stored_receipts(sf) == []


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (f"POST {CB} HTTP/1.1\r\nContent-Type: application/json\r\n\r\n", 411),
        (
            "POST /sms-status-receiver/v1/update-status HTTP/1.1\r\nTransfer-Encoding: chunked\r\n"
            "Content-Type: application/json\r\n\r\n",
            501,
        ),
        (
            f"POST {CB} HTTP/1.1\r\nContent-Type: application/json\r\n"
            "Content-Length: 999999\r\n\r\n",
            413,
        ),
        ("GET /sms-status-receiver/v1/update-status HTTP/1.1\r\n\r\n", 405),
        ("POST /nope HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n", 404),
        ("NONSENSE\r\n\r\n", 400),
    ],
    ids=["no-length", "chunked", "too-large", "get", "wrong-path", "garbage"],
)
async def test_tls_strict_http_handling(
    server: int, certs: Certs, head: str, expected: int
) -> None:
    payload = b"{}" if "Content-Length: 2" in head else b""
    status, _ = await post(server, client_ctx(certs, "pinned"), payload, headers=head)
    assert status == expected


def test_server_context_requires_client_certs_and_key_permissions(certs: Certs) -> None:
    import os

    ctx = build_server_ssl_context(
        certs["server_cert"], certs["server_key"], [certs["pinned_cert"]]
    )
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    os.chmod(certs["server_key"], 0o644)
    with pytest.raises(ValueError):
        build_server_ssl_context(certs["server_cert"], certs["server_key"], [certs["pinned_cert"]])
    with pytest.raises(ValueError):
        load_pins([])
    with pytest.raises(ValueError):  # neither a CA nor a pin: nothing to trust
        build_server_ssl_context(certs["server_cert"], certs["server_key"])
    loose = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with pytest.raises(ValueError):
        CallbackServer(ReceiptProcessor.__new__(ReceiptProcessor), loose)


# ------------------------------------------------ 8028 guide v1.1 (2026-11-12)

SECRET, PREVIOUS = "callback-secret-current", "callback-secret-previous"


def guide_body(secret: Any = SECRET, **kw: Any) -> bytes:
    """The callback exactly as guide 8.1 shows it: clientSecret, numeric sms_id,
    international phone number, ``status``."""
    data = {"clientSecret": secret, "sms_id": 17006100, "phone_number": "251900000001",
            "status": "Delivered", "event_time": "2026-09-01 08:12:19", **kw}
    return json.dumps({k: v for k, v in data.items() if v is not None}).encode()


def test_parse_guide_shape_and_status_field_only() -> None:
    r = parse_callback(guide_body(status="Pending"), T0)
    assert (r.provider_message_id, r.recipient, r.raw_status) == (
        "17006100", "+251900000001", "Pending")
    # 8028 confirmed the field is ``status``; the Postman ``sms_status`` is not read.
    postman = json.dumps({"sms_id": "17006100", "phone_number": PHONE,
                          "sms_status": "Delivered", "event_time": "2026-09-01 08:12:19"})
    assert parse_callback(postman.encode(), T0).raw_status == "null"
    assert parse_callback(guide_body(status=None), T0).raw_status == "null"
    nul = json.loads(guide_body())
    nul["status"] = None
    assert parse_callback(json.dumps(nul).encode(), T0).raw_status == "null"
    assert parse_callback(guide_body(status=""), T0).raw_status == "empty"


async def test_callback_secret_is_required_and_never_stored_or_logged(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await sent_sms(engine, sf, keys, repo, clock)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock,
                            callback_secrets=[SECRET, PREVIOUS])
    peer = der(certs["pinned_cert"])
    wrong = "wrong-secret-value"
    for bad in (guide_body(None), guide_body(wrong), guide_body(12345678), guide_body("")):
        r = await proc.handle(peer, "POST", DEFAULT_PATH, JSON, bad)
        assert r.status == 401 and json.loads(r.body) == {
            "success": False, "error": "authentication_failure"}
    assert await stored_receipts(sf) == []
    ok = await proc.handle(peer, "POST", DEFAULT_PATH, JSON, guide_body(status="Pending"))
    assert ok.status == 200 and json.loads(ok.body) == {"success": True}
    rotated = await proc.handle(peer, "POST", DEFAULT_PATH, JSON, guide_body(secret=PREVIOUS))
    assert rotated.status == 200
    dump = repr(await stored_receipts(sf))
    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    for secret in (SECRET, PREVIOUS, wrong):
        assert secret not in dump and secret not in logged
    with pytest.raises(ValueError):
        ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), callback_secrets=["short"])


async def test_pending_never_overwrites_a_final_status(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=clock)
    peer = der(certs["pinned_cert"])
    for st in ("Pending", "Delivered", "Pending"):  # guide 8.3: late Pending after Delivered
        r = await proc.handle(peer, "POST", DEFAULT_PATH, JSON, guide_body(status=st))
        assert r.status == 200
    assert (await status_of(sf, mid))["status"] == "delivered"
    statuses = [row["raw_status"] for row in await stored_receipts(sf)]
    assert statuses == ["Pending", "Delivered", "Pending"]


async def test_query_string_on_the_registered_path_is_accepted(
    repo: PostgresMessageRepository, certs: Certs
) -> None:
    proc = ReceiptProcessor(repo, load_pins([certs["pinned_cert"]]), clock=lambda: T0)
    r = await proc.handle(der(certs["pinned_cert"]), "POST", DEFAULT_PATH + "?v=1", JSON, body())
    assert r.status == 200


def test_processor_needs_pins_or_a_trusted_ca(repo: PostgresMessageRepository) -> None:
    with pytest.raises(ValueError):
        ReceiptProcessor(repo, frozenset())
    proc = ReceiptProcessor(repo, frozenset(), trust_ca=True)
    assert proc.is_authenticated(b"verified-by-tls") and not proc.is_authenticated(None)


@pytest.fixture
async def ca_server(
    repo: PostgresMessageRepository, certs: Certs, clock: FakeClock
) -> AsyncIterator[int]:
    """CA mode: trust 8028's callback CA, no pins, callback secret required."""
    ctx = build_server_ssl_context(
        certs["server_cert"], certs["server_key"], client_ca_certs=[certs["ca_cert"]]
    )
    proc = ReceiptProcessor(repo, trust_ca=True, callback_secrets=[SECRET], clock=clock)
    srv = CallbackServer(proc, ctx)
    port = await srv.start("127.0.0.1", 0)
    yield port
    await srv.close()


async def test_ca_mode_accepts_any_cert_from_the_8028_ca_and_refuses_others(
    ca_server: int,
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Keys,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    certs: Certs,
) -> None:
    mid = await sent_sms(engine, sf, keys, repo, clock)
    status, payload = await post(ca_server, client_ctx(certs, "unpinned"), guide_body())
    assert status == 200 and json.loads(payload) == {"success": True}
    assert (await status_of(sf, mid))["status"] == "delivered"
    status, _ = await post(ca_server, client_ctx(certs, "pinned"), guide_body("nope-nope"))
    assert status == 401
    for who in (None, "rogue"):  # no certificate, or one not issued by the CA
        assert await raw_exchange(ca_server, client_ctx(certs, who), guide_body()) == b""
    assert len(await stored_receipts(sf)) == 1
