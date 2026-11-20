"""API abuse against the composed service (real API + PostgreSQL). Complements the MSG-02
unit-level checks with credential-placement, type-confusion and echo probes, and verifies
that nothing reaches storage or the (mocked) gateway when a request is refused."""

from __future__ import annotations

import json
from typing import Any

import pytest
from itsupport import OTP_TEXT, Harness, fake_number, hdr, new_key, sms_body

pytestmark = pytest.mark.postgres


async def _nothing_persisted(h: Harness) -> None:
    assert (await h.one("SELECT count(*) FROM messages"))[0] == 0
    await h.worker().run_once()
    assert h.sms.calls == [] and h.tg.calls == []


@pytest.mark.parametrize(
    "variant",
    ["query", "basic", "no-scheme", "double-space", "two-tokens", "token-cookie", "empty"],
)
async def test_credentials_only_accepted_as_a_single_bearer_header(
    h: Harness, variant: str
) -> None:
    import base64

    token = await h.caller("esignet", bot=None)
    url = "/api/v1/messages/sms"
    headers: dict[str, str] = {"Content-Type": "application/json", "Idempotency-Key": new_key()}
    if variant == "query":
        url += f"?access_token={token}"
    elif variant == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(f"esignet:{token}".encode()).decode()
    elif variant == "no-scheme":
        headers["Authorization"] = token
    elif variant == "double-space":
        headers["Authorization"] = f"Bearer  {token}"
    elif variant == "two-tokens":
        headers["Authorization"] = f"Bearer {token} {token}"
    elif variant == "token-cookie":
        headers["Cookie"] = f"Authorization=Bearer {token}"
    else:
        headers["Authorization"] = "Bearer "
    r = await h.api.post(url, json=sms_body(h.clock), headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert token not in r.text
    await _nothing_persisted(h)


async def test_lowercase_scheme_is_accepted_per_rfc_9110(h: Harness) -> None:
    token = await h.caller("esignet", bot=None)
    r = await h.api.post(
        "/api/v1/messages/sms",
        json=sms_body(h.clock),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Idempotency-Key": new_key(),
        },
    )
    assert r.status_code == 202


SECRETISH = "0900000101-" + "482913"


@pytest.mark.parametrize(
    "body",
    [
        {"recipient": SECRETISH},
        {"recipient": 900000101},
        {"text": OTP_TEXT + "\x00" + SECRETISH},
        {"text": OTP_TEXT + SECRETISH * 10},
        {"source_reference": "otp:" + SECRETISH + " "},
        {"expires_at": "2026-09-01T05:20:00"},  # naive
        {"purpose": SECRETISH},
        {SECRETISH: "x"},
        {"recipient": ["0900000101"]},
        {"text": {"t": SECRETISH}},
    ],
    ids=lambda b: next(iter(b)) if next(iter(b)) != SECRETISH else "unknown-field",
)
async def test_invalid_values_are_rejected_without_echo(h: Harness, body: dict[str, Any]) -> None:
    token = await h.caller("esignet", bot=None)
    r = await h.post_sms(token, new_key(), **body)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"] == "application/problem+json"
    for needle in (SECRETISH, "482913", "900000101", OTP_TEXT):
        assert needle not in r.text
    await _nothing_persisted(h)


@pytest.mark.parametrize("chat_id", ["555000111", 555000111.0, True, 2**53, 0, None], ids=repr)
async def test_telegram_chat_id_type_confusion_is_rejected(h: Harness, chat_id: Any) -> None:
    token = await h.caller("grievance", bot="grievance")
    r = await h.post_tg(token, new_key(), chat_id=chat_id)
    assert r.status_code == 422
    await _nothing_persisted(h)


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/json; charset=latin-1", "application/x-www-form-urlencoded", ""],
)
async def test_non_json_media_types_refused(h: Harness, content_type: str) -> None:
    token = await h.caller("esignet", bot=None)
    headers = hdr(token, new_key())
    headers["Content-Type"] = content_type
    r = await h.api.post(
        "/api/v1/messages/sms", content=json.dumps(sms_body(h.clock)), headers=headers
    )
    assert r.status_code == 415
    await _nothing_persisted(h)


async def test_status_lookup_does_not_echo_or_enumerate(h: Harness) -> None:
    token = await h.caller("esignet", bot=None)
    for probe in (SECRETISH, "1 OR 1=1", "%27", "00000000-0000-0000-0000-000000000000"):
        r = await h.api.get(f"/api/v1/messages/{probe}", headers=hdr(token))
        assert r.status_code == 404 and SECRETISH not in r.text and "OR" not in r.text
    for method in ("PUT", "DELETE", "PATCH"):
        r = await h.api.request(method, "/api/v1/messages/sms", headers=hdr(token, new_key()))
        assert r.status_code == 405 and r.headers["content-type"] == "application/problem+json"


async def test_duplicate_json_keys_resolve_to_one_canonical_payload(h: Harness) -> None:
    """Informational: the API parses duplicate keys last-wins (json.loads). The stored
    payload, fingerprint and the gateway text all agree on that single interpretation, so
    there is no split between what is validated and what is sent."""
    token = await h.caller("esignet", bot=None)
    body = json.dumps(sms_body(h.clock))[:-1] + ', "text": "Your OAN code is 111111."}'
    key = new_key()
    r = await h.api.post("/api/v1/messages/sms", content=body, headers=hdr(token, key))
    assert r.status_code == 202
    replay = await h.post_sms(token, key, text="Your OAN code is 111111.")
    assert replay.status_code == 200
    await h.worker().run_once()
    assert [c.body["smstext"] for c in h.sms.calls] == ["Your OAN code is 111111."]
    assert h.sms.calls[0].body["phonenums"] == [fake_number(101)]
