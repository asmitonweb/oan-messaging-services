"""Client behaviour against MOCKED HTTP (httpx.MockTransport). Fake credentials only."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import BASE_URL, FAKE_TOKEN, T0, FakeClock, client_for, config
from oan_client import (
    ClientConfig,
    MessageStatus,
    OanConflict,
    OanProtocolError,
    OanRejected,
    OanUncertain,
    ReconciliationRequired,
    Secret,
    SmsDelivery,
    TelegramDelivery,
)

OTP_TEXT = "Your OAN code is 482913."
KEY = "6f1d2c3e-8a4b-4c5d-9e6f-7a8b9c0d1e2f"
MID = "0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10"


def otp(**kw: Any) -> SmsDelivery:
    base: dict[str, Any] = {
        "idempotency_key": KEY, "recipient": "0900000001", "text": OTP_TEXT,
        "source_reference": "esignet:otp:tx-1", "purpose": "otp",
        "expires_at": T0 + timedelta(minutes=5), "first_submitted_at": T0,
    }
    base.update(kw)
    return SmsDelivery(**base)


def accepted(status: int = 202, msg_status: str = "accepted") -> httpx.Response:
    return httpx.Response(status, json={
        "id": MID, "idempotency_key": KEY, "channel": "sms", "status": msg_status,
        "source_reference": "esignet:otp:tx-1", "created_at": "2026-09-01T05:12:13Z",
        "expires_at": "2026-09-01T05:17:13Z"})


def problem(status: int, code: str, errors: list[dict[str, str]] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"title": "x", "status": status, "code": code,
                                        "errors": errors or []},
                          headers={"content-type": "application/problem+json"})


def sequence(*responses: Any) -> Any:
    it = iter(responses)

    def handler(_r: httpx.Request) -> httpx.Response:
        nxt = next(it)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt  # type: ignore[no-any-return]
    return handler


# ------------------------------------------------------------- acceptance and replay


async def test_202_new_acceptance_request_shape(clock: FakeClock) -> None:
    c, rec = client_for(sequence(accepted(202)), clock)
    sub = await c.submit(otp())
    assert (sub.id, sub.status, sub.replayed) == (MID, "accepted", False)
    req = rec.requests[0]
    assert req.method == "POST" and str(req.url) == f"{BASE_URL}/api/v1/messages/sms"
    assert req.headers["idempotency-key"] == KEY
    assert req.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {
        "recipient": "0900000001", "text": OTP_TEXT, "source_reference": "esignet:otp:tx-1",
        "purpose": "otp", "expires_at": "2026-09-01T05:17:13+00:00"}
    await c.aclose()


async def test_200_identical_replay_is_reported_as_replayed(clock: FakeClock) -> None:
    c, _ = client_for(sequence(accepted(200, "sent")), clock)
    sub = await c.submit(otp())
    assert sub.replayed and sub.status == "sent" and sub.id == MID
    await c.aclose()


async def test_telegram_request_shape(clock: FakeClock) -> None:
    c, rec = client_for(sequence(accepted(202)), clock)
    await c.submit(TelegramDelivery(idempotency_key=KEY, chat_id=555000111, text="Hello",
                                    source_reference="g:1", purpose="transactional",
                                    expires_at=None, first_submitted_at=T0))
    assert str(rec.requests[0].url).endswith("/api/v1/messages/telegram")
    assert json.loads(rec.requests[0].content) == {
        "chat_id": 555000111, "text": "Hello", "source_reference": "g:1",
        "purpose": "transactional"}
    await c.aclose()


# ---------------------------------------------------------- safe, bounded retries


async def test_retries_reuse_identical_key_and_bytes_and_expiry(clock: FakeClock) -> None:
    c, rec = client_for(sequence(problem(503, "service_unavailable"),
                                 httpx.ReadTimeout("t"), httpx.ConnectError("c"),
                                 accepted(200)), clock)
    sub = await c.submit(otp())
    assert sub.replayed and len(rec.requests) == 4
    bodies = {r.content for r in rec.requests}
    keys = {r.headers["idempotency-key"] for r in rec.requests}
    assert len(bodies) == 1 and keys == {KEY}
    assert json.loads(rec.requests[-1].content)["expires_at"] == "2026-09-01T05:17:13+00:00"
    assert clock.slept == [0.5, 1.0, 2.0]  # exponential backoff (jitter pinned to max)
    await c.aclose()


async def test_lost_response_then_replay_returns_original_id(clock: FakeClock) -> None:
    stored: dict[str, str] = {}

    def server(request: httpx.Request) -> httpx.Response:
        key = request.headers["idempotency-key"]
        if key not in stored:
            stored[key] = MID  # committed ...
            raise httpx.ReadTimeout("response lost after commit")  # ... reply lost
        return accepted(200)

    c, rec = client_for(server, clock)
    sub = await c.submit(otp())
    assert sub.id == MID and sub.replayed and len(rec.requests) == 2
    await c.aclose()


@pytest.mark.parametrize("final", [problem(503, "service_unavailable"),
                                   httpx.ReadTimeout("t"), problem(502, "x"),
                                   httpx.RemoteProtocolError("dropped")])
async def test_uncertain_outcomes_are_bounded(clock: FakeClock, final: Any) -> None:
    c, rec = client_for(sequence(*([final] * 3)), clock, max_attempts=3)
    with pytest.raises(OanUncertain) as info:
        await c.submit(otp())
    assert len(rec.requests) == 3 and info.value.idempotency_key == KEY
    assert OTP_TEXT not in str(info.value) and "0900000001" not in str(info.value)
    await c.aclose()


async def test_api_rate_limit_waits_out_a_short_retry_after_exactly(clock: FakeClock) -> None:
    # Replaces the earlier test that expected 9999 s to be shortened to 30 s (MSG-06 P1).
    c, _ = client_for(sequence(httpx.Response(429, headers={"Retry-After": "7"}),
                               accepted(202)), clock)
    await c.submit(otp())
    assert clock.slept == [7.0]
    await c.aclose()


@pytest.mark.parametrize(
    ("resp", "exc", "code"),
    [
        (problem(409, "idempotency_conflict",
                 [{"field": "header.Idempotency-Key", "code": "payload_mismatch"}]),
         OanConflict, "idempotency_conflict"),
        (problem(422, "validation_failed", [{"field": "recipient", "code": "invalid_et_mobile"}]),
         OanRejected, "validation_failed"),
        (problem(401, "unauthenticated"), OanRejected, "unauthenticated"),
        (problem(403, "forbidden", [{"field": "purpose", "code": "purpose_not_permitted"}]),
         OanRejected, "forbidden"),
        (problem(413, "payload_too_large"), OanRejected, "payload_too_large"),
        (problem(415, "unsupported_media_type"), OanRejected, "unsupported_media_type"),
    ],
)
async def test_definite_rejections_are_never_retried(clock: FakeClock, resp: httpx.Response,
                                                     exc: type[Exception], code: str) -> None:
    c, rec = client_for(sequence(resp), clock)
    with pytest.raises(exc) as info:
        await c.submit(otp())
    assert len(rec.requests) == 1 and clock.slept == []
    assert isinstance(info.value, OanRejected) and info.value.code == code
    await c.aclose()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_are_refused_not_followed(clock: FakeClock, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oan-messaging.test":
            return httpx.Response(status, headers={"Location": "https://evil.invalid/x"})
        return accepted(202)

    c, rec = client_for(handler, clock)
    with pytest.raises(OanProtocolError):
        await c.submit(otp())
    assert [r.url.host for r in rec.requests] == ["oan-messaging.test"]
    await c.aclose()


async def test_guarantee_window_stops_automatic_retries(clock: FakeClock) -> None:
    c, rec = client_for(sequence(accepted(202)), clock)
    clock.now = T0 + timedelta(days=30)
    with pytest.raises(ReconciliationRequired):
        await c.submit(otp())
    assert rec.requests == []  # nothing sent: reconcile instead
    await c.aclose()


async def test_otp_after_its_own_expiry_is_still_a_same_key_replay_only(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(accepted(200, "sent")), clock)
    clock.now = T0 + timedelta(hours=1)  # OTP expired at T0+5m
    sub = await c.submit(otp())
    assert sub.replayed
    assert json.loads(rec.requests[0].content)["expires_at"] == "2026-09-01T05:17:13+00:00"
    await c.aclose()


# ---------------------------------------------------------------------- status


def status_body(status: str, *, channel: str = "sms", receipts: bool = True,
                final: bool = False, **extra: Any) -> dict[str, Any]:
    return {"id": MID, "idempotency_key": KEY, "channel": channel, "purpose": "otp",
            "source_reference": "esignet:otp:tx-1", "status": status, "final": final,
            "recipient_masked": "+2519******01", "delivery_receipts_supported": receipts,
            "attempts": 1, "created_at": "2026-09-01T05:12:13Z",
            "updated_at": "2026-09-01T05:12:14Z", **extra}


async def test_unknown_status_never_triggers_a_submission(clock: FakeClock) -> None:
    c, rec = client_for(sequence(httpx.Response(200, json=status_body(
        "unknown", failure_code="timeout", provider_status="timeout"))), clock)
    st = await c.get_status(MID)
    assert isinstance(st, MessageStatus) and st.status == "unknown"
    assert st.may_resubmit is False and "Do NOT submit again" in st.meaning
    assert [r.method for r in rec.requests] == ["GET"]
    await c.aclose()


async def test_telegram_sent_is_not_handset_delivery(clock: FakeClock) -> None:
    c, _ = client_for(sequence(httpx.Response(200, json=status_body(
        "sent", channel="telegram", receipts=False, recipient_masked="chat:*******11"))), clock)
    st = await c.get_status(MID)
    assert "NOT handset delivery" in st.meaning and "no delivery receipts" in st.meaning
    await c.aclose()


@pytest.mark.parametrize("status", ["accepted", "sending", "sent", "delivered", "failed",
                                    "expired", "unknown"])
async def test_every_status_has_an_explanation(clock: FakeClock, status: str) -> None:
    final = status in ("delivered", "failed", "expired")
    extra: dict[str, Any] = {"failure_code": "provider_rejected"} if status == "failed" else {}
    if status == "delivered":
        extra = {"delivered_at": "2026-09-01T05:12:19Z"}
    c, _ = client_for(sequence(httpx.Response(200, json=status_body(status, final=final,
                                                                     **extra))), clock)
    st = await c.get_status(MID)
    assert st.meaning and st.may_resubmit is False
    await c.aclose()


async def test_status_lookup_errors_are_safe(clock: FakeClock) -> None:
    c, rec = client_for(sequence(problem(404, "not_found")), clock)
    with pytest.raises(OanRejected) as info:
        await c.get_status(MID)
    assert info.value.status == 404 and len(rec.requests) == 1
    c2, rec2 = client_for(sequence(problem(503, "x"), httpx.Response(200, json=status_body(
        "sent"))), clock)
    assert (await c2.get_status(MID)).status == "sent" and len(rec2.requests) == 2
    with pytest.raises(ValueError):
        await c2.get_status("../../admin")
    await c.aclose()
    await c2.aclose()


# ------------------------------------------------------------------ configuration


def test_config_requires_https_and_bounded_settings() -> None:
    for base in ("http://oan.test", "https://u:p@oan.test", "https://oan.test?x=1", "ftp://x"):
        with pytest.raises(ValueError):
            ClientConfig(base_url=base, token=Secret(FAKE_TOKEN))
    for kw in ({"max_attempts": 0}, {"max_attempts": 11}, {"read_timeout": 0},
               {"backoff_base": 0}, {"guarantee_window": timedelta(0)}):
        with pytest.raises(ValueError):
            config(**kw)


async def test_tls_is_verified_and_redirects_disabled_in_the_real_client() -> None:
    from oan_client import OanClient

    c = OanClient(config())
    try:
        http = c._http  # noqa: SLF001
        assert http.follow_redirects is False and http.trust_env is False
        assert http.timeout.connect == 5.0 and http.timeout.read == 10.0
        pool = http._transport._pool  # type: ignore[attr-defined]  # noqa: SLF001
        assert pool._ssl_context.verify_mode.name == "CERT_REQUIRED"  # noqa: SLF001
        assert pool._ssl_context.check_hostname is True  # noqa: SLF001
    finally:
        await c.aclose()


@pytest.mark.parametrize("bad", [
    {"idempotency_key": "short"}, {"idempotency_key": "has space here"},
    {"purpose": "marketing"}, {"expires_at": None},
    {"expires_at": T0.replace(tzinfo=None)}, {"source_reference": "bad ref!"},
])
def test_delivery_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        otp(**bad)


# ---------------------------------------------------------------------- redaction


async def test_no_body_recipient_otp_or_token_in_logs_errors_or_reprs(
    clock: FakeClock, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    c, _ = client_for(sequence(problem(503, "x"), httpx.ReadTimeout("t"),
                               problem(422, "validation_failed",
                                       [{"field": "recipient", "code": "invalid_et_mobile"}])),
                      clock)
    d = otp()
    with pytest.raises(OanRejected) as info:
        await c.submit(d)
    shown = " ".join([caplog.text, str(info.value), repr(d), repr(c), repr(config()),
                      repr(Secret(FAKE_TOKEN)), "".join(str(r.args) for r in caplog.records)])
    for needle in (OTP_TEXT, "482913", "0900000001", FAKE_TOKEN, FAKE_TOKEN[7:], KEY):
        assert needle not in shown
    assert "attempt=3" in caplog.text
    await c.aclose()
