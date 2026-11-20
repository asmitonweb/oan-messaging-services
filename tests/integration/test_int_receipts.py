"""8028 receipts through the REAL TLS ingress against messages dispatched by the real
worker and 8028 adapter (gateway HTTP mocked). Includes a callback delivered while the
gateway call is still in flight, i.e. before the worker has stored the sms_id."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from itsupport import Certs, Harness, callback, fake_number, new_key, start_ingress

pytestmark = pytest.mark.postgres


async def _sent_sms(h: Harness, token: str, n: int = 101) -> str:
    r = await h.post_sms(
        token, new_key(), recipient=fake_number(n), purpose="transactional", expires_at=None
    )
    return str(r.json()["id"])


async def test_callback_arriving_during_the_gateway_call_is_applied(
    h: Harness, certs: Certs
) -> None:
    ingress = await start_ingress(h.repo, certs, h.clock)
    seen: list[tuple[int | None, str]] = []

    async def gateway(body: dict[str, Any], _n: int) -> httpx.Response:
        # 8028 queues and delivers before its HTTP response reaches the worker.
        status, _ = await ingress.post(
            callback("88001", body["phonenums"][0], "Delivered", h.clock())
        )
        rows = await h.rows("SELECT disposition FROM receipts")
        seen.append((status, rows[0][0]))
        return h.sms.ok(body, sms_id="88001")

    h.sms.script = gateway
    try:
        token = await h.caller("grievance", bot=None)
        mid = await _sent_sms(h, token)
        await h.worker().run_once()
    finally:
        await ingress.server.close()
    assert seen == [(200, "orphaned")]  # acknowledged only after it was durable
    view = await h.status(token, mid)
    assert view["status"] == "delivered"
    rows = await h.rows("SELECT disposition, message_id::text FROM receipts")
    assert rows == [("applied", mid)]


async def test_duplicate_and_out_of_order_callbacks_never_regress(h: Harness, certs: Certs) -> None:
    token = await h.caller("grievance", bot=None)
    mid = await _sent_sms(h, token)
    await h.worker().run_once()
    sms_id = h.sms.returned_ids[0]
    t = h.clock()
    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        sequence = [
            ("Delivered", t + timedelta(seconds=20)),
            ("Sent", t + timedelta(seconds=5)),  # older event, arrives later
            ("Delivered", t + timedelta(seconds=20)),  # 8028 retry of the same event
            ("Rejected", t + timedelta(seconds=30)),  # contradicts a final state
            ("Failed", t + timedelta(seconds=31)),  # undocumented status
        ]
        for raw, at in sequence:
            status, _ = await ingress.post(callback(sms_id, fake_number(101), raw, at))
            assert status == 200
    finally:
        await ingress.server.close()
    view = await h.status(token, mid)
    assert (view["status"], view["provider_status"]) == ("delivered", "Delivered")
    rows = await h.rows("SELECT raw_status, disposition FROM receipts ORDER BY received_at, id")
    assert sorted(r[0] for r in rows) == ["Delivered", "Delivered", "Failed", "Rejected", "Sent"]
    assert sum(1 for r in rows if r[1] == "applied") == 1
    assert len(h.sms.calls) == 1


async def test_receipt_for_another_number_is_mismatched(h: Harness, certs: Certs) -> None:
    token = await h.caller("grievance", bot=None)
    mid = await _sent_sms(h, token)
    await h.worker().run_once()
    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        status, _ = await ingress.post(
            callback(h.sms.returned_ids[0], fake_number(999), "Delivered", h.clock())
        )
    finally:
        await ingress.server.close()
    assert status == 200
    assert (await h.status(token, mid))["status"] == "sent"
    assert (await h.one("SELECT disposition FROM receipts"))[0] == "mismatched"


@pytest.mark.parametrize("who", [None, "sibling", "rogue"])
async def test_spoofed_certificate_headers_cannot_deliver_a_message(
    h: Harness, certs: Certs, who: str | None
) -> None:
    token = await h.caller("grievance", bot=None)
    mid = await _sent_sms(h, token)
    await h.worker().run_once()
    pinned_pem = certs["pinned_cert"].read_text().replace("\n", "")
    spoof = (
        f"X-SSL-Client-Cert: {pinned_pem}\r\nX-Client-Verify: SUCCESS\r\n"
        'X-Forwarded-Client-Cert: Hash=00;Subject="CN=it-fake-8028-callback"\r\n'
        "X-Forwarded-For: 127.0.0.1\r\n"
    )
    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        status, _ = await ingress.post(
            callback(h.sms.returned_ids[0], fake_number(101), "Delivered", h.clock()),
            who=who,
            extra_headers=spoof,
        )
    finally:
        await ingress.server.close()
    assert status is None  # refused in the TLS handshake: no HTTP exchange at all
    assert (await h.status(token, mid))["status"] == "sent"
    assert (await h.one("SELECT count(*) FROM receipts"))[0] == 0


async def test_pinning_a_ca_certificate_still_rejects_other_certificates_it_issued(
    h: Harness, certs: Certs
) -> None:
    """Deployment mistake guard: if the pinned file were the issuing CA (not the 8028
    client certificate), a sibling certificate from that CA passes the handshake but the
    in-process SHA-256 pin check still refuses it with 403 and stores nothing."""
    token = await h.caller("grievance", bot=None)
    mid = await _sent_sms(h, token)
    await h.worker().run_once()
    ingress = await start_ingress(h.repo, certs, h.clock, pinned=("ca",))
    try:
        status, body = await ingress.post(
            callback(h.sms.returned_ids[0], fake_number(101), "Delivered", h.clock()),
            who="sibling",
        )
    finally:
        await ingress.server.close()
    assert status == 403 and json.loads(body) == {"success": False, "error": "forbidden"}
    assert (await h.status(token, mid))["status"] == "sent"
    assert (await h.one("SELECT count(*) FROM receipts"))[0] == 0


async def test_unacknowledged_when_storage_is_down_then_accepted_on_8028_retry(
    h: Harness, certs: Certs, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await h.caller("grievance", bot=None)
    mid = await _sent_sms(h, token)
    await h.worker().run_once()
    real = h.repo.record_receipt

    async def down(*_a: Any, **_k: Any) -> Any:
        from sqlalchemy.exc import OperationalError

        raise OperationalError("INSERT", {}, Exception("db down"))

    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        body = callback(h.sms.returned_ids[0], fake_number(101), "Delivered", h.clock())
        monkeypatch.setattr(h.repo, "record_receipt", down)
        status, payload = await ingress.post(body)
        assert (status, json.loads(payload)) == (503, {"success": False, "error": "retry_later"})
        monkeypatch.setattr(h.repo, "record_receipt", real)
        status, _ = await ingress.post(body)  # 8028 retry schedule: 1 min later
        assert status == 200
    finally:
        await ingress.server.close()
    assert (await h.status(token, mid))["status"] == "delivered"
