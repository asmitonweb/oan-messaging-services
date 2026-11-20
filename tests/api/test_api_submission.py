"""Submission, validation, authentication, permission and isolation tests (real PostgreSQL)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import (
    T0,
    FakeClock,
    Secrets,
    headers,
    make_caller,
    new_key,
    sms_body,
    tg_body,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import Channel, Purpose, SubmissionResponse
from oan_messaging.storage.schema import api_credentials, message_content, messages


async def count_messages(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as s:
        return int((await s.execute(select(func.count()).select_from(messages))).scalar_one())


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["status"] == status and body["code"] == code
    return body


# ----------------------------------------------------------------------- acceptance


async def test_sms_otp_accepted_202_and_persisted_before_response(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    resp = await client.post("/api/v1/messages/sms", json=sms_body(), headers=headers(token, key))
    assert resp.status_code == 202, resp.text
    body = SubmissionResponse.model_validate(resp.json())
    assert body.status.value == "accepted" and body.idempotency_key == key
    assert resp.headers["location"] == f"/api/v1/messages/{body.id}"
    # Committed: visible from an independent session after the response.
    async with sf() as s:
        row = (await s.execute(select(messages).where(messages.c.id == body.id))).mappings().one()
        content = (
            (
                await s.execute(
                    select(message_content).where(message_content.c.message_id == body.id)
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "accepted" and row["recipient_masked"] == "+2519******01"
    assert row["priority"] == 0 and row["payload_fp_digest"] is not None
    assert content["ciphertext"] and len(content["nonce"]) == 12


async def test_get_status_view_has_no_text_and_masks_recipient(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/sms", json=sms_body(), headers=headers(token, new_key())
    )
    got = await client.get(f"/api/v1/messages/{r.json()['id']}", headers=headers(token))
    assert got.status_code == 200
    view = got.json()
    assert view["status"] == "accepted" and view["final"] is False
    assert view["delivery_receipts_supported"] is True and view["attempts"] == 0
    assert "482913" not in got.text and "0900000001" not in got.text
    assert view["recipient_masked"] == "+2519******01"
    assert "failure_code" not in view  # exclude_none per OpenAPI


async def test_telegram_accepted_and_never_claims_delivery_receipts(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "grievance-svc", bot="grievance")
    r = await client.post(
        "/api/v1/messages/telegram", json=tg_body(), headers=headers(token, new_key())
    )
    assert r.status_code == 202, r.text
    view = (await client.get(f"/api/v1/messages/{r.json()['id']}", headers=headers(token))).json()
    assert view["channel"] == "telegram" and view["delivery_receipts_supported"] is False
    assert view["recipient_masked"] == "chat:*******11"


async def test_shared_source_reference_with_two_keys_creates_two_messages(  # A2
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "grievance-svc")
    ref = "grievance:G-1042:update:3"
    a = await client.post(
        "/api/v1/messages/sms",
        headers=headers(token, new_key()),
        json=sms_body(
            recipient="0900000003", source_reference=ref, purpose="transactional", expires_at=None
        ),
    )
    b = await client.post(
        "/api/v1/messages/sms",
        headers=headers(token, new_key()),
        json=sms_body(
            recipient="0900000004", source_reference=ref, purpose="transactional", expires_at=None
        ),
    )
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["id"] != b.json()["id"]
    assert await count_messages(sf) == 2


async def test_key_scope_is_caller_and_channel(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    t1 = await make_caller(sf, keys, "svc-a")
    t2 = await make_caller(sf, keys, "svc-b")
    key = new_key()
    r1 = await client.post("/api/v1/messages/sms", json=sms_body(), headers=headers(t1, key))
    r2 = await client.post("/api/v1/messages/sms", json=sms_body(), headers=headers(t2, key))
    r3 = await client.post("/api/v1/messages/telegram", json=tg_body(), headers=headers(t1, key))
    assert [r.status_code for r in (r1, r2, r3)] == [202, 202, 202]
    assert len({r.json()["id"] for r in (r1, r2, r3)}) == 3


# --------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "recipient",
    ["091200000", "0111000000", "+251111000000", "12345", "09000000011", "+1 202 555 0100", ""],
)
async def test_invalid_numbers_rejected_without_echo(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    recipient: str,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/sms",
        headers=headers(token, new_key()),
        json=sms_body(recipient=recipient, purpose="alert", expires_at=None),
    )
    body = assert_problem(r, 422, "validation_failed")
    assert {"field": "recipient", "code": "invalid_et_mobile"} in body["errors"]
    if recipient:
        assert recipient not in r.text
    assert await count_messages(sf) == 0


async def test_missing_otp_expiry_rejected(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/sms", headers=headers(token, new_key()), json=sms_body(expires_at=None)
    )
    body = assert_problem(r, 422, "validation_failed")
    assert body["errors"] == [{"field": "expires_at", "code": "required_for_otp"}]
    assert "482913" not in r.text


@pytest.mark.parametrize(
    ("expires", "code"),
    [
        (T0 - timedelta(seconds=1), "not_in_future"),
        (T0, "not_in_future"),
        (T0 + timedelta(minutes=31), "beyond_ceiling"),
    ],
)
async def test_expired_or_too_far_otp_rejected_for_new_acceptance(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    expires: Any,
    code: str,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/sms",
        headers=headers(token, new_key()),
        json=sms_body(expires_at=expires.isoformat()),
    )
    body = assert_problem(r, 422, "validation_failed")
    assert body["errors"] == [{"field": "expires_at", "code": code}]
    assert await count_messages(sf) == 0


async def test_expiry_is_never_extended(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    exp = "2026-09-01T08:13:13.123456+03:00"  # 05:13:13.123456Z
    r = await client.post(
        "/api/v1/messages/sms", headers=headers(token, new_key()), json=sms_body(expires_at=exp)
    )
    assert r.json()["expires_at"] == "2026-09-01T05:13:13.123456Z"


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        (sms_body(expires_at="2026-09-01T05:17:13"), "expires_at", "timezone_required"),
        (
            sms_body(text="a" * 460, purpose="alert", expires_at=None),
            "text",
            "too_long_for_sms",
        ),
        (
            sms_body(text="ሀ" * 202, purpose="broadcast", expires_at=None),
            "text",
            "too_long_for_sms",
        ),
        (sms_body(text="a" * 161), "text", "too_long_for_single_sms"),
        (sms_body(text="ሀ" * 71), "text", "too_long_for_single_sms"),
        (sms_body(text="   "), "text", "blank"),
        (sms_body(purpose="marketing"), "purpose", "enum"),
        (sms_body(source_reference="bad ref!"), "source_reference", "invalid_format"),
        (sms_body(source_reference=None), "source_reference", "required"),
    ],
)
async def test_body_validation_errors_are_redacted(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    body: dict[str, Any],
    field: str,
    code: str,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post("/api/v1/messages/sms", headers=headers(token, new_key()), json=body)
    problem = assert_problem(r, 422, "validation_failed")
    assert {"field": field, "code": code} in problem["errors"]
    for v in body.values():
        if isinstance(v, str) and len(v) > 3 and v not in ("otp", "alert"):
            assert v not in r.text
    assert "input" not in r.text and "msg" not in r.text


async def test_unknown_field_name_is_not_echoed(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/telegram",
        headers=headers(token, new_key()),
        json=tg_body(bot_token="123456789:AAsecretsecretsecretsecretsecret00"),  # noqa: S106
    )
    problem = assert_problem(r, 422, "validation_failed")
    assert {"code": "unknown_field"} in problem["errors"]
    assert "bot_token" not in r.text and "AAsecret" not in r.text


@pytest.mark.parametrize("key", [None, "short", "has space in it", "x" * 129, "-leading-dash"])
async def test_idempotency_key_header_required_and_validated(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    key: str | None,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post("/api/v1/messages/sms", headers=headers(token, key), json=sms_body())
    problem = assert_problem(r, 422, "validation_failed")
    assert problem["errors"][0]["field"] == "header.Idempotency-Key"
    assert await count_messages(sf) == 0


async def test_key_in_body_rejected(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/sms",
        headers=headers(token, new_key()),
        json=sms_body(idempotency_key=new_key()),
    )
    assert_problem(r, 422, "validation_failed")


async def test_media_type_size_and_json_errors(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    h = headers(token, new_key())
    r = await client.post(
        "/api/v1/messages/sms", headers={**h, "Content-Type": "text/plain"}, content=b"{}"
    )
    assert_problem(r, 415, "unsupported_media_type")
    r = await client.post("/api/v1/messages/sms", headers=h, content=b"{" + b" " * 20000 + b"}")
    assert_problem(r, 413, "payload_too_large")
    r = await client.post("/api/v1/messages/sms", headers=h, content=b"not json 482913")
    body = assert_problem(r, 422, "validation_failed")
    assert body["errors"] == [{"code": "invalid_json"}] and "482913" not in r.text
    r = await client.post("/api/v1/messages/sms", headers=h, content=b"[1,2]")
    assert_problem(r, 422, "validation_failed")
    assert await count_messages(sf) == 0


# ------------------------------------------------------------ auth and permissions


@pytest.mark.parametrize(
    "auth", [None, "Bearer", "Bearer nope", "Basic abc", "bearer  x y", "Bearer oanmsg_" + "A" * 43]
)
async def test_unauthenticated_requests_rejected(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    auth: str | None,
) -> None:
    await make_caller(sf, keys, "esignet")
    h = {"Content-Type": "application/json", "Idempotency-Key": new_key()}
    if auth is not None:
        h["Authorization"] = auth
    r = await client.post("/api/v1/messages/sms", headers=h, json=sms_body())
    assert_problem(r, 401, "unauthenticated")
    assert r.headers["www-authenticate"] == "Bearer"
    g = await client.get("/api/v1/messages/0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10", headers=h)
    assert_problem(g, 401, "unauthenticated")
    assert await count_messages(sf) == 0


async def test_revoked_and_disabled_callers_rejected(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    from oan_messaging.storage.credentials import revoke_credentials, upsert_caller

    t1 = await make_caller(sf, keys, "svc-a")
    await revoke_credentials(sf, "svc-a", T0)
    r = await client.post("/api/v1/messages/sms", headers=headers(t1, new_key()), json=sms_body())
    assert_problem(r, 401, "unauthenticated")
    t2 = await make_caller(sf, keys, "svc-b")
    await upsert_caller(
        sf, "svc-b", channels=frozenset(Channel), purposes=frozenset(Purpose), enabled=False
    )
    r = await client.post("/api/v1/messages/sms", headers=headers(t2, new_key()), json=sms_body())
    assert_problem(r, 401, "unauthenticated")


async def test_credentials_are_issued_separately_and_stored_only_as_hmac(
    sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    t1 = await make_caller(sf, keys, "svc-a")
    t2 = await make_caller(sf, keys, "svc-b")
    assert t1 != t2 and t1.startswith("oanmsg_")
    async with sf() as s:
        rows = (await s.execute(select(api_credentials))).mappings().all()
    blob = repr([dict(r) for r in rows])
    assert t1 not in blob and t2 not in blob and t1[7:] not in blob
    assert all(len(r["token_hmac"]) == 32 for r in rows)


async def test_channel_purpose_and_bot_permissions(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    sms_only = await make_caller(
        sf,
        keys,
        "auth-svc",
        channels=frozenset({Channel.SMS}),
        purposes=frozenset({Purpose.TRANSACTIONAL}),
        bot=None,
    )
    r = await client.post(
        "/api/v1/messages/telegram", headers=headers(sms_only, new_key()), json=tg_body()
    )
    assert assert_problem(r, 403, "forbidden")["errors"] == [{"code": "channel_not_permitted"}]
    r = await client.post(
        "/api/v1/messages/sms", headers=headers(sms_only, new_key()), json=sms_body()
    )  # purpose otp not allowed
    assert assert_problem(r, 403, "forbidden")["errors"] == [
        {"field": "purpose", "code": "purpose_not_permitted"}
    ]
    no_bot = await make_caller(sf, keys, "da-svc", bot=None)
    r = await client.post(
        "/api/v1/messages/telegram", headers=headers(no_bot, new_key()), json=tg_body()
    )
    assert assert_problem(r, 403, "forbidden")["errors"] == [
        {"code": "telegram_bot_not_configured"}
    ]
    assert await count_messages(sf) == 0


async def test_cross_caller_isolation_and_unknown_ids_are_404(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    owner = await make_caller(sf, keys, "svc-a")
    other = await make_caller(sf, keys, "svc-b")
    r = await client.post(
        "/api/v1/messages/sms", headers=headers(owner, new_key()), json=sms_body()
    )
    mid = r.json()["id"]
    assert (await client.get(f"/api/v1/messages/{mid}", headers=headers(owner))).status_code == 200
    for path in (
        f"/api/v1/messages/{mid}",
        "/api/v1/messages/0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10",
        "/api/v1/messages/not-a-uuid",
    ):
        resp = await client.get(path, headers=headers(other))
        assert_problem(resp, 404, "not_found")
    # The other caller's 404 is byte-identical for existing and missing ids.
    a = await client.get(f"/api/v1/messages/{mid}", headers=headers(other))
    b = await client.get(
        "/api/v1/messages/0b9f3a52-5c1e-4d55-9b7a-3f1c2a7e9d10", headers=headers(other)
    )
    assert a.content == b.content


async def test_unknown_routes_and_methods_use_problem_format(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    assert_problem(await client.get("/nope", headers=headers(token)), 404, "not_found")
    r = await client.delete("/api/v1/messages/sms", headers=headers(token))
    assert_problem(r, 405, "validation_failed")


async def test_clock_is_server_side(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    client: httpx.AsyncClient,
    clock: FakeClock,
) -> None:
    token = await make_caller(sf, keys, "esignet")
    r = await client.post(
        "/api/v1/messages/sms", headers=headers(token, new_key()), json=sms_body()
    )
    assert r.json()["created_at"] == "2026-09-01T05:12:13Z"
    assert clock.now == T0
