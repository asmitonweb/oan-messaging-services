"""Composed path: real API -> PostgreSQL -> worker -> real 8028/Telegram adapters (provider
HTTP mocked) -> real TLS receipt ingress -> GET status. Distinguishes accepted, sent,
delivered, failed, expired and unknown exactly as contract 3.0.0 defines them."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from itsupport import (
    OTP_TEXT,
    Certs,
    Harness,
    callback,
    e164,
    eat,
    fake_number,
    http_status,
    new_key,
    start_ingress,
)
from sqlalchemy.ext.asyncio import create_async_engine

from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.worker import run_cleanup

pytestmark = pytest.mark.postgres


async def test_sms_otp_accepted_then_sent_then_delivered_over_real_tls(
    h: Harness, certs: Certs, database_url: str
) -> None:
    token = await h.caller("esignet", bot=None)
    r = await h.post_sms(token, key := new_key())
    assert r.status_code == 202, r.text
    mid = r.json()["id"]
    assert r.json()["status"] == "accepted"

    # Durable acceptance BEFORE any provider submission: row and encrypted content are
    # committed and visible to a brand-new engine; the gateway was never contacted.
    fresh = make_engine(database_url)
    try:
        async with make_session_factory(fresh)() as s:
            from sqlalchemy import text

            row = (
                await s.execute(text("SELECT status FROM messages WHERE id=:i"), {"i": mid})
            ).one()
            content = (
                await s.execute(
                    text("SELECT count(*) FROM message_content WHERE message_id=:i"), {"i": mid}
                )
            ).scalar_one()
        assert row[0] == "accepted" and content == 1
    finally:
        await fresh.dispose()
    assert h.sms.calls == []
    view = await h.status(token, mid)
    assert view["status"] == "accepted" and view["final"] is False
    assert view["delivery_receipts_supported"] is True and "sent_at" not in view

    stats = await h.worker().run_once()
    assert stats.dispatched == 1
    [call] = h.sms.calls
    # The real adapter built the 8028 body: byte-exact text, domestic number, EAT time.
    assert call.body == {
        "clientSecret": h.keys.sms_secret,
        "smstext": OTP_TEXT,
        "phonenums": [fake_number(101)],
        "pushdatetime": eat(h.clock()),
    }
    view = await h.status(token, mid)
    # sent = 8028 queued it. It is NOT a delivery claim.
    assert view["status"] == "sent" and view["final"] is False
    assert view["provider_status"] == "http_200" and "delivered_at" not in view
    assert (await h.one("SELECT count(*) FROM message_content"))[0] == 0  # purged at sent

    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        event = h.clock() + timedelta(seconds=40)
        status, _ = await ingress.post(
            callback(h.sms.returned_ids[0], fake_number(101), "Delivered", event)
        )
        assert status == 200
    finally:
        await ingress.server.close()
    view = await h.status(token, mid)
    assert view["status"] == "delivered" and view["final"] is True
    assert view["delivered_at"].startswith(event.strftime("%Y-%m-%dT%H:%M:%S"))
    # Same-key replay after delivery: 200, original id, no new provider call.
    r = await h.post_sms(token, key)
    assert r.status_code == 200 and r.json()["id"] == mid and r.json()["status"] == "delivered"
    await h.worker("w2").run_once()
    assert len(h.sms.calls) == 1


async def test_telegram_sent_is_never_delivered(h: Harness, certs: Certs) -> None:
    token = await h.caller("grievance", bot="grievance")
    r = await h.post_tg(token, new_key())
    assert r.status_code == 202
    mid = r.json()["id"]
    await h.worker().run_once()
    [call] = h.tg.calls
    assert h.tg.bot_of(call) == "grievance"
    assert call.body == {"chat_id": 555000111, "text": "Grievance G-1 updated."}
    view = await h.status(token, mid)
    assert view["status"] == "sent" and view["final"] is False
    assert view["delivery_receipts_supported"] is False and "delivered_at" not in view
    # Even a well-formed, authenticated 8028 callback carrying the Telegram message_id
    # cannot touch a Telegram message: it is stored as an orphan.
    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        status, _ = await ingress.post(callback("301", fake_number(101), "Delivered", h.clock()))
    finally:
        await ingress.server.close()
    assert status == 200
    assert (await h.status(token, mid))["status"] == "sent"
    assert (await h.one("SELECT disposition FROM receipts"))[0] == "orphaned"


def _raise(exc: Exception) -> Any:
    def script(_b: dict[str, Any], _n: int) -> httpx.Response:
        raise exc

    return script


AMBIGUOUS = {
    "http_503": (lambda b, n: http_status(503), "http_503", "provider_server_error"),
    "http_429": (lambda b, n: http_status(429), "http_429", "rate_limited"),
    "read_timeout": (_raise(httpx.ReadTimeout("timed out")), "timeout", "timeout"),
    "dropped": (
        _raise(httpx.RemoteProtocolError("closed")),
        "transport_error",
        "malformed_response",
    ),
    "empty_data": (
        lambda b, n: httpx.Response(200, json={"success": True, "data": []}),
        "http_200",
        "malformed_response",
    ),
    "other_phone": (
        lambda b, n: httpx.Response(
            200, json={"success": True, "data": [{"phone_number": "0900000999", "sms_id": "5"}]}
        ),
        "http_200",
        "malformed_response",
    ),
    "redirect": (
        lambda b, n: httpx.Response(302, headers={"Location": "https://elsewhere.invalid/"}),
        "http_302",
        "malformed_response",
    ),
}


async def test_http_500_queued_nothing_and_is_retried(h: Harness) -> None:
    """8028 confirmed (20 Nov 2026) that a 500 queues nothing: the message is retried with
    the normal backoff and sent once 8028 accepts it; it is never ``unknown``."""
    h.sms.script = lambda b, n: http_status(500) if n == 1 else h.sms.ok(b)
    token = await h.caller("auth", bot=None)
    r = await h.post_sms(token, new_key(), purpose="transactional", expires_at=None,
                         text="Reset requested.")
    mid = r.json()["id"]
    await h.worker().run_once()
    assert (await h.status(token, mid))["status"] == "accepted"  # waiting for its retry
    h.clock.advance(seconds=31)
    await h.worker().run_once()
    view = await h.status(token, mid)
    assert view["status"] == "sent" and len(h.sms.calls) == 2


@pytest.mark.parametrize("case", sorted(AMBIGUOUS))
async def test_ambiguous_gateway_outcome_is_unknown_and_never_resent(h: Harness, case: str) -> None:
    script, provider_status, failure = AMBIGUOUS[case]
    h.sms.script = script
    token = await h.caller("auth", bot=None)
    key = new_key()
    body = {"purpose": "transactional", "expires_at": None, "text": "Reset requested."}
    r = await h.post_sms(token, key, **body)
    mid = r.json()["id"]
    await h.worker().run_once()
    view = await h.status(token, mid)
    assert (view["status"], view["provider_status"], view["failure_code"]) == (
        "unknown",
        provider_status,
        failure,
    )
    assert view["final"] is False and "sent_at" not in view

    # Hours later, after many worker cycles, cleanup and a same-key API retry (which is
    # an API replay, not a gateway resend): still exactly one gateway call.
    h.sms.script = None
    for _ in range(3):
        h.clock.advance(hours=1)
        await h.worker("w2").run_once()
        await run_cleanup(h.repo, h.clock())
    r = await h.post_sms(token, key, **body)
    assert r.status_code == 200 and r.json()["id"] == mid and r.json()["status"] == "unknown"
    await h.worker("w3").run_once()
    assert len(h.sms.calls) == 1
    assert (await h.one("SELECT count(*) FROM messages"))[0] == 1
    assert (await h.one("SELECT count(*) FROM message_content"))[0] == 0


async def test_definite_rejection_is_failed_and_final(h: Harness) -> None:
    h.sms.script = lambda b, n: http_status(
        400, {"success": False, "message": "Invalid phone number"}
    )
    token = await h.caller("auth", bot=None)
    mid = (await h.post_sms(token, new_key())).json()["id"]
    await h.worker().run_once()
    view = await h.status(token, mid)
    assert (view["status"], view["failure_code"], view["final"]) == (
        "failed",
        "invalid_recipient",
        True,
    )
    h.clock.advance(minutes=1)
    await h.worker().run_once()
    assert len(h.sms.calls) == 1


async def test_provably_unsent_connection_failure_is_retried_once_with_backoff(
    h: Harness,
) -> None:
    attempts: list[int] = []

    def script(b: dict[str, Any], n: int) -> httpx.Response:
        attempts.append(n)
        if n == 1:
            raise httpx.ConnectError("connection refused")
        return h.sms.ok(b)

    h.sms.script = script
    token = await h.caller("auth", bot=None)
    mid = (await h.post_sms(token, new_key())).json()["id"]
    await h.worker().run_once()
    view = await h.status(token, mid)
    assert view["status"] == "accepted" and view["attempts"] == 1
    await h.worker().run_once()  # backoff not elapsed: no call
    assert attempts == [1]
    h.clock.advance(seconds=31)
    await h.worker().run_once()
    view = await h.status(token, mid)
    assert view["status"] == "sent" and view["attempts"] == 2
    # Same original text and number on the retry, still within the original OTP expiry.
    assert h.sms.calls[0].body["smstext"] == h.sms.calls[1].body["smstext"] == OTP_TEXT
    assert h.sms.calls[1].body["phonenums"] == [fake_number(101)]


@pytest.mark.parametrize(
    ("raw", "code"),
    [("Rejected", "provider_rejected"), ("Expired", "provider_delivery_expired")],
)
async def test_negative_receipts_fail_the_message(
    h: Harness, certs: Certs, raw: str, code: str
) -> None:
    token = await h.caller("grievance", bot=None)
    mid = (await h.post_sms(token, new_key(), purpose="transactional", expires_at=None)).json()[
        "id"
    ]
    await h.worker().run_once()
    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        status, _ = await ingress.post(
            callback(h.sms.returned_ids[0], fake_number(101), raw, h.clock())
        )
    finally:
        await ingress.server.close()
    assert status == 200
    view = await h.status(token, mid)
    assert (view["status"], view["failure_code"], view["provider_status"]) == ("failed", code, raw)


async def test_simulated_restart_between_acceptance_and_dispatch(
    h: Harness, database_url: str
) -> None:
    """SIMULATED restart (new engine, repository and worker objects in the same process).
    Genuine process interruption is covered by test_int_process.py."""
    token = await h.caller("auth", bot=None)
    mid = (await h.post_sms(token, new_key(), recipient=e164(fake_number(102)))).json()["id"]
    await h.engine.dispose()
    engine2 = create_async_engine(database_url, hide_parameters=True)
    try:
        sf2 = make_session_factory(engine2)
        h.repo = h.keys.repo(sf2)
        h.engine = engine2
        await h.worker("after-restart").run_once()
    finally:
        await engine2.dispose()
    assert h.sms.calls[0].body["phonenums"] == [fake_number(102)]
    assert (await h.status(token, mid))["status"] == "sent"
