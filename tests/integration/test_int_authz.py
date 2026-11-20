"""Caller and Telegram-bot authorization across the composed path: the API decides who
may submit, storage binds the authorized bot, and the worker re-checks at dispatch."""

from __future__ import annotations

import pytest
from itsupport import Harness, hdr, new_key, tg_body

from oan_messaging.contracts import Channel, Purpose
from oan_messaging.storage.credentials import revoke_credentials, upsert_caller

pytestmark = pytest.mark.postgres


async def test_each_caller_dispatches_only_through_its_configured_bot(h: Harness) -> None:
    g = await h.caller("grievance", bot="grievance")
    d = await h.caller("da-services", bot="da")
    gid = (await h.post_tg(g, new_key(), chat_id=700001)).json()["id"]
    did = (await h.post_tg(d, new_key(), chat_id=700002)).json()["id"]
    await h.worker().run_once()
    used = {c.body["chat_id"]: h.tg.bot_of(c) for c in h.tg.calls}
    assert used == {700001: "grievance", 700002: "da"}
    assert (await h.status(g, gid))["status"] == (await h.status(d, did))["status"] == "sent"


@pytest.mark.parametrize("field", ["bot", "bot_token", "token", "api_base_url", "url", "priority"])
async def test_request_cannot_choose_bot_token_url_or_priority(h: Harness, field: str) -> None:
    g = await h.caller("grievance", bot="grievance")
    r = await h.api.post(
        "/api/v1/messages/telegram",
        json=tg_body(**{field: h.keys.bot_tokens["da"]}),
        headers=hdr(g, new_key()),
    )
    assert r.status_code == 422
    assert h.keys.bot_tokens["da"] not in r.text and field not in r.text
    assert (await h.one("SELECT count(*) FROM messages"))[0] == 0


async def test_bot_remapped_after_acceptance_is_held_never_retargeted(h: Harness) -> None:
    g = await h.caller("grievance", bot="grievance")
    mid = (await h.post_tg(g, new_key())).json()["id"]
    await upsert_caller(
        h.sf,
        "grievance",
        channels=frozenset(Channel),
        purposes=frozenset(Purpose),
        telegram_bot="da",
    )
    stats = await h.worker().run_once()
    assert h.tg.calls == [] and stats.claimed == 0
    assert (await h.status(g, mid))["status"] == "accepted"
    await upsert_caller(
        h.sf,
        "grievance",
        channels=frozenset(Channel),
        purposes=frozenset(Purpose),
        telegram_bot="grievance",
    )
    await h.worker().run_once()
    assert [h.tg.bot_of(c) for c in h.tg.calls] == ["grievance"]


async def test_disabled_caller_is_held_and_revoked_credential_cannot_query(h: Harness) -> None:
    token = await h.caller("auth", bot=None)
    mid = (await h.post_sms(token, new_key(), purpose="transactional", expires_at=None)).json()[
        "id"
    ]
    await upsert_caller(
        h.sf, "auth", channels=frozenset(Channel), purposes=frozenset(Purpose), enabled=False
    )
    await h.worker().run_once()
    assert h.sms.calls == []
    assert (await h.api.get(f"/api/v1/messages/{mid}", headers=hdr(token))).status_code == 401
    await upsert_caller(h.sf, "auth", channels=frozenset(Channel), purposes=frozenset(Purpose))
    await h.worker().run_once()
    assert len(h.sms.calls) == 1
    # Revoking the credential stops the API; it does not recall accepted work.
    await revoke_credentials(h.sf, "auth", h.clock())
    r = await h.post_sms(token, new_key())
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert (await h.api.get(f"/api/v1/messages/{mid}", headers=hdr(token))).status_code == 401


async def test_cross_caller_status_is_indistinguishable_from_unknown(h: Harness) -> None:
    a = await h.caller("auth", bot=None)
    b = await h.caller("grievance", bot=None)
    key = new_key()
    mid = (await h.post_sms(a, key)).json()["id"]
    other = await h.api.get(f"/api/v1/messages/{mid}", headers=hdr(b))
    missing = await h.api.get(
        "/api/v1/messages/00000000-0000-4000-8000-000000000000", headers=hdr(b)
    )
    assert other.status_code == missing.status_code == 404
    assert other.content == missing.content
    # The same key under another caller is an independent delivery, not a replay of A's.
    r = await h.post_sms(b, key)
    assert r.status_code == 202 and r.json()["id"] != mid


async def test_channel_and_purpose_permissions_block_before_persistence(h: Harness) -> None:
    token = await h.caller(
        "esignet",
        bot=None,
        channels=frozenset({Channel.SMS}),
        purposes=frozenset({Purpose.OTP}),
    )
    assert (await h.post_tg(token, new_key())).status_code == 403
    r = await h.post_sms(token, new_key(), purpose="broadcast", expires_at=None)
    assert r.status_code == 403 and r.json()["errors"][0]["code"] == "purpose_not_permitted"
    no_bot = await h.caller("nobot", bot=None)
    r = await h.post_tg(no_bot, new_key())
    assert r.status_code == 403 and r.json()["errors"][0]["code"] == "telegram_bot_not_configured"
    assert (await h.one("SELECT count(*) FROM messages"))[0] == 0
