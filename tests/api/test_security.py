"""Encryption at rest, log/error redaction, startup validation and no provider calls."""

from __future__ import annotations

import ast
import base64
import logging
import secrets
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    ROOT,
    FakeClock,
    Secrets,
    headers,
    make_caller,
    make_client,
    make_repo,
    new_key,
    sms_body,
    tg_body,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.api import create_app
from oan_messaging.storage.config import ServiceSecrets, StorageSettings, load_policy
from oan_messaging.storage.crypto import (
    ContentCipher,
    EncryptedContent,
    KeyringError,
    parse_keyring,
)
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_content, messages

SMS = "/api/v1/messages/sms"
SECRET_TEXT = "Your OAN code is 482913."  # noqa: S105 (fake test message)


async def test_content_is_encrypted_at_rest_and_bound_to_its_row(
    client: httpx.AsyncClient, sf: async_sessionmaker[AsyncSession], keys: Secrets
) -> None:
    token = await make_caller(sf, keys, "esignet")
    a = await client.post(SMS, json=sms_body(), headers=headers(token, new_key()))
    b = await client.post(SMS, json=sms_body(), headers=headers(token, new_key()))
    t = await client.post(
        "/api/v1/messages/telegram", json=tg_body(), headers=headers(token, new_key())
    )
    async with sf() as s:
        content = [dict(r) for r in (await s.execute(select(message_content))).mappings()]
        meta = [dict(r) for r in (await s.execute(select(messages))).mappings()]
    raw = b"".join(bytes(r["ciphertext"]) + bytes(r["nonce"]) for r in content)
    dump = repr(meta).encode() + raw
    for needle in (
        SECRET_TEXT,
        "482913",
        "0900000001",
        "+251900000001",
        "900000001",
        "555000111",
        "Grievance G-1 updated",
    ):
        assert needle.encode() not in dump
        assert needle.encode("utf-16-le") not in raw
    by_id = {r["message_id"]: r for r in content}
    ida, idb, idt = (uuid.UUID(x.json()["id"]) for x in (a, b, t))
    assert by_id[ida]["ciphertext"] != by_id[idb]["ciphertext"]  # random nonces
    blob = EncryptedContent(
        by_id[ida]["key_version"], by_id[ida]["nonce"], by_id[ida]["ciphertext"]
    )
    assert keys.cipher.decrypt(ida, "sms", blob) == {
        "text": SECRET_TEXT,
        "recipient": "+251900000001",
    }
    with pytest.raises(KeyringError):
        keys.cipher.decrypt(idb, "sms", blob)  # moved onto another row: rejected
    with pytest.raises(KeyringError):
        keys.cipher.decrypt(ida, "telegram", blob)
    assert (
        keys.cipher.decrypt(
            idt,
            "telegram",
            EncryptedContent(
                by_id[idt]["key_version"], by_id[idt]["nonce"], by_id[idt]["ciphertext"]
            ),
        )["chat_id"]
        == 555000111
    )


async def test_logs_never_contain_secrets_text_recipients_or_fingerprints(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    async with make_client(repo, sf, keys, clock) as c:
        await c.post(SMS, json=sms_body(), headers=headers(token, key))
        await c.post(SMS, json=sms_body(), headers=headers(token, key))  # replay
        await c.post(
            SMS, json=sms_body(text="Your OAN code is 777111."), headers=headers(token, key)
        )  # 409
        await c.post(
            SMS, json=sms_body(recipient="0111000000"), headers=headers(token, new_key())
        )  # 422
        await c.post(SMS, json=sms_body(), headers=headers(token + "x", new_key()))  # 401
        await c.post("/api/v1/messages/telegram", json=tg_body(), headers=headers(token, new_key()))
    dead = make_engine("postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none")
    async with make_client(make_repo(make_session_factory(dead), keys), sf, keys, clock) as c:
        await c.post(SMS, json=sms_body(), headers=headers(token, new_key()))  # 503
    await dead.dispose()
    async with sf() as s:
        digests = [r[0] for r in (await s.execute(select(messages.c.payload_fp_digest)))]
    logged = caplog.text + " ".join(str(r.args) for r in caplog.records)
    forbidden = [
        SECRET_TEXT,
        "482913",
        "777111",
        "0900000001",
        "+251900000001",
        "0111000000",
        "555000111",
        token,
        token[7:],
        "hunter2",
        base64.b64encode(keys.pepper).decode(),
        *filter(None, digests),
    ]
    for needle in forbidden:
        assert needle not in logged, needle
    assert "request method=POST route=/api/v1/messages/sms status=202" in logged
    assert "error_type=" in logged  # the 503 was logged by type only


def settings_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    env = {
        "OAN_MSG_DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:1/x",
        "OAN_MSG_CONTENT_KEYS": "c1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "OAN_MSG_CONTENT_KEY_CURRENT": "c1",
        "OAN_MSG_FINGERPRINT_KEYS": "f1:" + base64.b64encode(secrets.token_bytes(32)).decode(),
        "OAN_MSG_FINGERPRINT_KEY_CURRENT": "f1",
        "OAN_MSG_CREDENTIAL_PEPPER": base64.b64encode(secrets.token_bytes(32)).decode(),
        **extra,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize(
    "name",
    [
        "MAX_OTP_TTL",
        "MAX_EXPIRY_HORIZON",
        "DEDUP_WINDOW",
        "RECEIPT_WINDOW",
        "RECEIPT_EVIDENCE_RETENTION",
        "PURGE_SWEEP_INTERVAL",
        "WORKER_LEASE",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
async def test_a21_non_positive_policy_fails_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    settings_env(monkeypatch, **{f"OAN_MSG_POLICY_{name}_SECONDS": value})
    with pytest.raises(ValidationError):
        load_policy(StorageSettings())
    app = create_app()
    with pytest.raises(ValidationError):
        async with app.router.lifespan_context(app):
            pass


async def test_valid_settings_build_and_secrets_are_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_env(monkeypatch, OAN_MSG_POLICY_DEDUP_WINDOW_SECONDS=str(40 * 86400))
    s = StorageSettings()
    assert load_policy(s).dedup_window.days == 40
    sec = ServiceSecrets.from_settings(s)
    for shown in (repr(s), str(s), repr(sec), repr(sec.cipher)):
        assert s.content_keys.get_secret_value() not in shown
        assert s.credential_pepper.get_secret_value() not in shown
        assert "u:p@" not in shown


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "c1",
        "c1:not-base64!",
        "c1:" + base64.b64encode(b"x" * 8).decode(),
        "BAD VERSION:" + base64.b64encode(b"x" * 32).decode(),
    ],
)
def test_keyring_errors_do_not_echo_material(spec: str) -> None:
    with pytest.raises(KeyringError) as info:
        parse_keyring(spec)
    material = spec.partition(":")[2]
    if material:
        assert material not in str(info.value)


def test_content_cipher_requires_aes256_keys() -> None:
    with pytest.raises(KeyringError):
        ContentCipher({"c1": b"x" * 16}, "c1")
    with pytest.raises(KeyringError):
        ContentCipher({"c1": b"x" * 32}, "c2")


def test_api_package_never_imports_provider_transports() -> None:
    api_dir = ROOT / "oan_messaging" / "api"
    for path in api_dir.rglob("*.py"):
        tree = ast.parse(Path(path).read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "adapters" not in n and not n.startswith(("httpx", "requests")), (path, n)


async def test_handlers_make_no_outbound_calls_during_full_flow(
    client: httpx.AsyncClient,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
) -> None:
    # The autouse fixture makes any Python-level outbound connect raise; a full
    # submit/replay/status flow therefore proves handlers never contact a provider.
    token = await make_caller(sf, keys, "esignet")
    key = new_key()
    r: Any = await client.post(SMS, json=sms_body(), headers=headers(token, key))
    assert r.status_code == 202
    assert (await client.post(SMS, json=sms_body(), headers=headers(token, key))).status_code == 200
    assert (
        await client.get(f"/api/v1/messages/{r.json()['id']}", headers=headers(token))
    ).status_code == 200
