"""Concurrent duplicates, conflicting content and lost API responses, followed through the
worker to the (mocked) gateway: whatever the API sees, the gateway sees one submission per
intended delivery. Lost responses here are SIMULATED (the client discards the response);
test_int_process.py kills a real server process between commit and response."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from itsupport import OTP_TEXT, Harness, fake_number, new_key

pytestmark = pytest.mark.postgres


async def test_concurrent_identical_requests_one_row_one_gateway_send(h: Harness) -> None:
    token = await h.caller("esignet", bot=None)
    key = new_key()
    responses = await asyncio.gather(*(h.post_sms(token, key) for _ in range(25)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 24 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert (await h.one("SELECT count(*) FROM messages"))[0] == 1
    # Two workers race for it: one gateway submission.
    await asyncio.gather(h.worker("w1").run_once(), h.worker("w2").run_once())
    assert len(h.sms.calls) == 1


async def test_concurrent_conflicting_bodies_one_winner_rest_409(h: Harness) -> None:
    token = await h.caller("esignet", bot=None)
    key = new_key()
    texts = [f"Your OAN code is 4829{i:02d}." for i in range(10)]
    responses = await asyncio.gather(*(h.post_sms(token, key, text=t) for t in texts))
    winners = [(t, r) for t, r in zip(texts, responses, strict=True) if r.status_code == 202]
    assert len(winners) == 1
    assert sorted(r.status_code for r in responses) == [202] + [409] * 9
    assert all(
        r.json()["code"] == "idempotency_conflict" for r in responses if r.status_code == 409
    )
    await h.worker().run_once()
    [call] = h.sms.calls
    assert call.body["smstext"] == winners[0][0]  # exactly the winning body, once


async def test_lost_response_retries_replay_without_resend_or_expiry_change(h: Harness) -> None:
    token = await h.caller("esignet", bot=None)
    key = new_key()
    original_expiry = (h.clock() + timedelta(minutes=5)).isoformat()
    lost = await h.post_sms(token, key, expires_at=original_expiry)
    assert lost.status_code == 202  # committed; pretend the caller never saw it
    del lost

    await h.worker().run_once()  # delivery proceeds while the caller is retrying
    h.clock.advance(minutes=6)  # past the OTP expiry: replay must not become 422

    r = await h.post_sms(token, key, expires_at=original_expiry)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert r.json()["expires_at"].startswith(original_expiry[:19])

    # A retry that tries to extend the OTP expiry or change the text is a conflict:
    # OAN never re-arms an OTP and never accepts a replacement under the same key.
    later = (h.clock() + timedelta(minutes=5)).isoformat()
    assert (await h.post_sms(token, key, expires_at=later)).status_code == 409
    assert (
        await h.post_sms(token, key, expires_at=original_expiry, text="Your OAN code is 000000.")
    ).status_code == 409
    # Equivalent recipient spelling is the same delivery.
    r = await h.post_sms(token, key, expires_at=original_expiry, recipient="+251900000101")
    assert r.status_code == 200
    await h.worker().run_once()
    assert len(h.sms.calls) == 1 and h.sms.calls[0].body["smstext"] == OTP_TEXT


async def test_same_source_reference_two_keys_are_two_deliveries(h: Harness) -> None:
    token = await h.caller("grievance", bot=None)
    ids = set()
    for i in (201, 202, 203):
        r = await h.post_sms(
            token,
            new_key(),
            recipient=fake_number(i),
            purpose="transactional",
            expires_at=None,
            source_reference="grievance:G-77:ack",
            text="Grievance G-77 received.",
        )
        assert r.status_code == 202
        ids.add(r.json()["id"])
    await h.worker().run_once()
    assert len(ids) == 3
    assert sorted(c.body["phonenums"][0] for c in h.sms.calls) == [
        fake_number(201),
        fake_number(202),
        fake_number(203),
    ]
