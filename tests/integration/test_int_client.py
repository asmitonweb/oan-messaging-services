"""MSG-06 client and outbox example against the REAL accepted API, PostgreSQL repository,
worker and adapters (MSG-07 cross-module coverage).

Only the network is simulated (``itclient.FaultTransport`` over ``httpx.ASGITransport``)
and provider HTTP is mocked (``itsupport.Fake8028`` / ``FakeTelegramApi``). Every request
the transport forwards is handled by the accepted FastAPI application and committed to
PostgreSQL. Real sockets and a real API process: ``test_int_client_process.py``.

Client and service share one simulated clock, so retries, ``Retry-After`` waits, the
guarantee window and OTP expiry are deterministic.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from itclient import (
    LogCapture,
    assert_clean,
    echoing_error,
    exception_surface,
    gateway_status,
    lose_response,
    make_rig,
    persist_and_reload,
    refuse_connection,
    sensitive_needles,
    stored_then_gateway_status,
)
from itsupport import OTP_TEXT, Harness, callback, fake_number, http_status
from oan_client import (
    OanConflict,
    OanRejected,
    OanRetryDeferred,
    OanUncertain,
    ReconciliationRequired,
    SmsDelivery,
    TelegramDelivery,
    delivery_key,
)
from outbox_dispatch import OutboxRow, dispatch, is_due, poll_sweep, reconcile

from oan_messaging.receipts import parse_callback

pytestmark = pytest.mark.postgres


def otp_delivery(
    h: Harness,
    *,
    key: str | None = None,
    n: int = 101,
    minutes: int = 5,
    first: datetime | None = None,
    text: str = OTP_TEXT,
) -> SmsDelivery:
    now = h.clock()
    return SmsDelivery(
        idempotency_key=key or str(uuid.uuid4()),
        recipient=fake_number(n),
        text=text,
        source_reference="auth:password-reset:req-1",
        purpose="otp",
        expires_at=now + timedelta(minutes=minutes),
        first_submitted_at=first or now,
    )


def outbox_row(h: Harness, n: int = 201, **kw: object) -> OutboxRow:
    base: dict[str, object] = {
        "delivery_id": str(uuid.uuid4()),
        "recipient": fake_number(n),
        "text": "Grievance G-1042 received.",
        "source_reference": "grievance:G-1042:acknowledged",
        "purpose": "transactional",
        "expires_at": None,
        "first_submitted_at": h.clock(),
    }
    base.update(kw)
    return OutboxRow(**base)  # type: ignore[arg-type]


async def db_messages(h: Harness, key: str | None = None) -> list[tuple[object, ...]]:
    sql = "SELECT id::text, caller_id, status, expires_at, source_reference FROM messages"
    if key is None:
        return [tuple(r) for r in await h.rows(sql + " ORDER BY created_at")]
    return [tuple(r) for r in await h.rows(sql + " WHERE idempotency_key = :k", k=key)]


async def deliver_receipt(h: Harness, sms_id: str, n: int, status: str) -> str:
    body = json.dumps(callback(sms_id, fake_number(n), status, h.clock())).encode()
    disposition = await h.repo.record_receipt(parse_callback(body, h.clock()), h.clock())
    return disposition.value


@pytest.fixture
def logs() -> Iterator[LogCapture]:
    handler = LogCapture()
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


# ------------------------------------------------------------ 1. lost responses


async def test_lost_response_after_commit_is_replayed_to_the_original_message(
    h: Harness,
) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token)
    rig.transport.faults = [lose_response]
    d = otp_delivery(h)
    try:
        sub = await rig.client.submit(d)
    finally:
        await rig.aclose()
    rows = await db_messages(h, d.idempotency_key)
    assert len(rows) == 1 and sub.id == rows[0][0]
    assert sub.replayed is True and sub.status == "accepted"
    posts = [s for s in rig.transport.seen if s.method == "POST"]
    assert len(posts) == 2 and all(s.forwarded for s in posts)  # both reached PostgreSQL
    assert len({s.content for s in posts}) == 1  # byte-identical body
    assert {s.key for s in posts} == {d.idempotency_key}  # identical key
    await h.worker().run_once()
    assert len(h.sms.calls) == 1  # one gateway submission for the delivery


async def test_every_attempt_lost_then_later_sweep_returns_the_original(h: Harness) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    rig.transport.faults = [lose_response] * 4  # default max_attempts
    row = outbox_row(h)
    try:
        await dispatch(rig.client, row, h.clock())
        assert row.state == "queued" and row.oan_message_id is None
        assert rig.transport.forwarded("POST") == 4
        stored = await db_messages(h, row.delivery_id)
        assert len(stored) == 1  # stored on the first attempt, although no answer arrived
        row = persist_and_reload(row)
        assert row.next_attempt_at is not None
        h.clock.now = row.next_attempt_at
        await dispatch(rig.client, row, h.clock())
    finally:
        await rig.aclose()
    assert (row.state, row.oan_message_id) == ("accepted", stored[0][0])
    assert len(await db_messages(h)) == 1


async def test_refused_connection_never_reached_the_service_then_accepted_once(
    h: Harness,
) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token)
    rig.transport.faults = [refuse_connection]
    try:
        sub = await rig.client.submit(otp_delivery(h))
    finally:
        await rig.aclose()
    assert (sub.replayed, rig.transport.forwarded("POST")) == (False, 1)
    assert len(await db_messages(h)) == 1


# ------------------------------------------------------------ 2. original OTP expiry


async def test_otp_expiry_is_unchanged_across_retries_and_replay_after_expiry(
    h: Harness,
) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token)
    d = otp_delivery(h, minutes=5)
    rig.transport.faults = [lose_response, stored_then_gateway_status(503)]
    try:
        first = await rig.client.submit(d)
        assert first.replayed is True
        assert datetime.fromisoformat(first.expires_at or "") == d.expires_at
        h.clock.advance(minutes=10)  # the OTP has expired; nothing was dispatched
        await h.worker().run_once()
        after = await rig.client.submit(d)  # identical replay after expiry
        status = await rig.client.get_status(first.id)
    finally:
        await rig.aclose()
    assert (after.id, after.replayed, after.status) == (first.id, True, "expired")
    assert datetime.fromisoformat(after.expires_at or "") == d.expires_at  # not 422
    assert (status.status, status.final, status.failure_code) == (
        "expired",
        True,
        "expired_before_send",
    )
    rows = await db_messages(h)
    assert len(rows) == 1 and rows[0][3] == d.expires_at
    assert h.sms.calls == []  # never sent after its deadline
    bodies = {s.content for s in rig.transport.seen if s.method == "POST"}
    assert len(bodies) == 1  # every retry resent the original expires_at byte-for-byte


# --------------------------------------------- 3. caller isolation and scoped keys


async def test_caller_isolation_and_caller_scoped_idempotency(h: Harness) -> None:
    auth_token = await h.caller("auth", bot=None)
    grievance_token = await h.caller("grievance", bot=None)
    a, g = make_rig(h, auth_token), make_rig(h, grievance_token)
    shared_key = str(uuid.uuid4())
    d = otp_delivery(h, key=shared_key)
    try:
        sa = await a.client.submit(d)
        sg = await g.client.submit(d)  # same key, same body, another caller
        assert (sa.replayed, sg.replayed) == (False, False) and sa.id != sg.id
        with pytest.raises(OanRejected) as cross:
            await g.client.get_status(sa.id)
        assert (cross.value.status, cross.value.code) == (404, "not_found")
        with pytest.raises(OanRejected) as unknown_id:
            await g.client.get_status(str(uuid.uuid4()))
        assert unknown_id.value.status == 404  # indistinguishable from a foreign id
        changed = otp_delivery(h, key=shared_key, text="Your OAN code is 000000.")
        with pytest.raises(OanConflict):
            await a.client.submit(changed)
        assert (await a.client.submit(d)).id == sa.id
    finally:
        await a.aclose()
        await g.aclose()
    rows = await db_messages(h)
    assert sorted(str(r[1]) for r in rows) == ["auth", "grievance"]


# ---------------------------------------- 4. bounded retries and Retry-After defer


async def test_uncertain_outcomes_are_retried_a_bounded_number_of_times(h: Harness) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token)
    rig.transport.faults = [stored_then_gateway_status(503)] * 4
    d = otp_delivery(h)
    try:
        with pytest.raises(OanUncertain) as exc:
            await rig.client.submit(d)
        assert type(exc.value) is OanUncertain and exc.value.attempts == 4
        assert rig.transport.count("POST") == 4
        assert rig.slept == [0.5, 1.0, 2.0]  # exponential backoff, capped, bounded
        later = await rig.client.submit(d)  # caller's later retry, same stored delivery
    finally:
        await rig.aclose()
    rows = await db_messages(h)
    assert len(rows) == 1 and (later.id, later.replayed) == (rows[0][0], True)


async def test_short_retry_after_is_waited_out_in_full(h: Harness) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token)
    rig.transport.faults = [gateway_status(503, retry_after="7")]
    try:
        sub = await rig.client.submit(otp_delivery(h))
    finally:
        await rig.aclose()
    assert rig.slept == [7.0] and sub.replayed is False


async def test_long_retry_after_defers_and_is_never_shortened_across_reload(
    h: Harness,
) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    rig.transport.faults = [stored_then_gateway_status(503, retry_after="3600")]
    row = outbox_row(h)
    t0 = h.clock()
    try:
        with pytest.raises(OanRetryDeferred) as exc:
            await rig.client.submit(row.to_delivery())
        assert exc.value.retry_not_before == t0 + timedelta(seconds=3600)
        assert rig.slept == [] and rig.transport.count("POST") == 1
        rig.transport.faults = [stored_then_gateway_status(503, retry_after="3600")]
        await dispatch(rig.client, row, t0)
        assert row.state == "queued" and row.next_attempt_at == t0 + timedelta(seconds=3600)
        row = persist_and_reload(row)
        assert row.next_attempt_at == t0 + timedelta(seconds=3600)
        for minutes in (5, 30, 59):  # sweeps before Retry-After send nothing
            h.clock.now = t0 + timedelta(minutes=minutes)
            assert not is_due(row, h.clock())
            await dispatch(rig.client, row, h.clock())
        assert rig.transport.count("POST") == 2
        h.clock.now = t0 + timedelta(seconds=3600)
        await dispatch(rig.client, row, h.clock())
    finally:
        await rig.aclose()
    stored = await db_messages(h, row.delivery_id)
    assert (row.state, row.oan_message_id) == ("accepted", stored[0][0])
    assert len(await db_messages(h)) == 1


# ------------------------------------------------------- 5. guarantee-window cutoff


async def test_no_submission_after_the_guarantee_window(h: Harness) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    old = h.clock() - timedelta(days=30, seconds=1)
    row = outbox_row(h, first_submitted_at=old)
    try:
        with pytest.raises(ReconciliationRequired):
            await rig.client.submit(row.to_delivery())
        await dispatch(rig.client, row, h.clock())
    finally:
        await rig.aclose()
    assert (row.state, row.needs_attention) == ("reconcile", True)
    assert rig.transport.seen == []  # nothing was sent at all
    assert await db_messages(h) == []


async def test_retry_after_reaching_past_the_window_stops_automatic_retries(
    h: Harness,
) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    first = h.clock() - timedelta(days=30) + timedelta(hours=1)
    rig.transport.faults = [stored_then_gateway_status(503, retry_after="7200")]
    try:
        with pytest.raises(ReconciliationRequired) as exc:
            await rig.client.submit(outbox_row(h, first_submitted_at=first).to_delivery())
    finally:
        await rig.aclose()
    assert exc.value.retry_not_before == h.clock() + timedelta(seconds=7200)
    assert rig.transport.count("POST") == 1 and rig.slept == []


# ------------------------------------------------------------------ 6. safe errors


@pytest.mark.parametrize("status", [400, 422, 503])
async def test_errors_never_carry_otps_recipients_bodies_or_tokens(
    h: Harness, logs: LogCapture, status: int
) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token, max_attempts=2)
    rig.transport.faults = [echoing_error(status)] * 2
    d = otp_delivery(h, n=111)
    try:
        with pytest.raises((OanRejected, OanUncertain)) as exc:
            await rig.client.submit(d)
    finally:
        await rig.aclose()
    needles = sensitive_needles(
        texts=[OTP_TEXT, "482913"], numbers=[fake_number(111)], tokens=[token]
    )
    assert_clean(exception_surface(exc.value), needles, "client exception")
    assert_clean(logs.text, needles, "logs")
    if isinstance(exc.value, OanRejected):
        assert exc.value.code == "unrecognized"  # echoed code reduced to a safe value
        assert all(c in ("unrecognized", "blank") for _f, c in exc.value.errors)


async def test_real_api_validation_error_reaches_the_client_without_values(
    h: Harness, logs: LogCapture
) -> None:
    token = await h.caller("auth", bot=None)
    rig = make_rig(h, token)
    bad = SmsDelivery(
        idempotency_key=str(uuid.uuid4()),
        recipient="0812345678",
        text=OTP_TEXT,
        source_reference="auth:password-reset:req-2",
        purpose="otp",
        expires_at=h.clock() + timedelta(minutes=5),
        first_submitted_at=h.clock(),
    )
    try:
        with pytest.raises(OanRejected) as exc:
            await rig.client.submit(bad)
    finally:
        await rig.aclose()
    assert (exc.value.status, exc.value.code) == (422, "validation_failed")
    assert ("recipient", "invalid_et_mobile") in exc.value.errors
    needles = sensitive_needles(
        texts=[OTP_TEXT, "482913", "0812345678", "812345678"], numbers=[], tokens=[token]
    )
    assert_clean(exception_surface(exc.value), needles, "client exception")
    assert_clean(logs.text, needles, "logs")
    assert rig.transport.count("POST") == 1  # a definite rejection is never retried


# ------------------------------------------------ 7. status and outbox reconciliation


async def test_client_status_matches_the_service_for_every_outcome(h: Harness) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    outcomes = {201: "sent", 202: "delivered", 203: "failed", 204: "unknown"}

    def gateway(body: dict[str, Any], _n: int) -> httpx.Response:
        n = int(str(body["phonenums"][0])[-3:])
        if n == 203:
            return http_status(400, {"success": False, "message": "Invalid phone number"})
        if n == 204:
            return http_status(503)
        return h.sms.ok(body)

    h.sms.script = gateway
    ids: dict[int, str] = {}
    try:
        for n in [*outcomes, 205]:
            delivery = outbox_row(
                h, n=n, expires_at=(h.clock() + timedelta(minutes=1)) if n == 205 else None
            ).to_delivery()
            ids[n] = (await rig.client.submit(delivery)).id
        assert (await rig.client.get_status(ids[201])).status == "accepted"
        h.clock.advance(minutes=2)  # 205's 1-minute deadline passes before any worker runs
        await h.worker().run_once()  # 201-204 dispatched; 205 expired, never attempted
        # Same-instant messages are claimed in id order, so look the sms_id up.
        sms_id = (
            await h.one("SELECT provider_message_id FROM messages WHERE id = :i", i=ids[202])
        )[0]
        assert await deliver_receipt(h, sms_id, 202, "Delivered") == "applied"
        views = {n: await rig.client.get_status(mid) for n, mid in ids.items()}
    finally:
        await rig.aclose()
    expected = {**outcomes, 205: "expired"}
    for n, view in views.items():
        db = (await h.one("SELECT status FROM messages WHERE id = :i", i=ids[n]))[0]
        assert view.status == db == expected[n]
        assert view.final is (expected[n] in ("delivered", "failed", "expired"))
        assert view.may_resubmit is False
    assert "NOT handset delivery" in views[201].meaning
    assert "Do NOT submit again" in views[204].meaning


async def test_outbox_reconciliation_persists_next_poll_at_across_reloads(h: Harness) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    row = outbox_row(h, n=211)
    t0 = h.clock()
    try:
        await dispatch(rig.client, row, t0)
        assert (row.state, row.oan_status, row.next_poll_at) == ("accepted", "accepted", t0)
        await reconcile(rig.client, row, t0)
        assert row.next_poll_at == t0 + timedelta(minutes=1) and row.polls == 1
        row = persist_and_reload(row)  # restart of the owning service
        assert row.next_poll_at == t0 + timedelta(minutes=1)

        h.clock.advance(seconds=10)
        await h.worker().run_once()  # the real worker sends it through the mocked 8028
        h.clock.now = t0 + timedelta(seconds=30)
        assert await poll_sweep(rig.client, [row], h.clock()) == 0  # not due: no GET
        assert rig.transport.count("GET") == 1

        assert row.next_poll_at is not None
        h.clock.now = row.next_poll_at
        await reconcile(rig.client, row, h.clock())
        assert (row.oan_status, row.oan_final) == ("sent", False)
        assert row.next_poll_at == h.clock() + timedelta(minutes=2)
        row = persist_and_reload(row)

        sms_id = h.sms.returned_ids[0]
        assert await deliver_receipt(h, sms_id, 211, "Delivered") == "applied"
        assert row.next_poll_at is not None
        h.clock.now = row.next_poll_at
        assert await poll_sweep(rig.client, [row], h.clock()) == 1
        assert (row.oan_status, row.oan_final) == ("delivered", True)
        assert row.next_poll_at is None  # polling ends at the final status
        row = persist_and_reload(row)
        h.clock.advance(days=1)
        assert await poll_sweep(rig.client, [row], h.clock()) == 0  # final: polling ends
        await dispatch(rig.client, row, h.clock())  # an accepted row is never resubmitted
    finally:
        await rig.aclose()
    assert (rig.transport.count("POST"), rig.transport.count("GET")) == (1, 3)
    assert row.state == "accepted" and len(h.sms.calls) == 1


# ---------------------------------------------- 8. unknown is never resubmitted


async def test_unknown_message_is_never_requeued_for_submission(h: Harness) -> None:
    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    h.sms.script = lambda _b, _n: http_status(503)  # ambiguous gateway outcome
    row = outbox_row(h, n=221)
    try:
        await dispatch(rig.client, row, h.clock())
        h.clock.advance(seconds=5)
        await h.worker().run_once()
        for _ in range(40):  # ten days of sweeps, restarts and polls
            h.clock.advance(hours=6)
            row = persist_and_reload(row)
            await dispatch(rig.client, row, h.clock())
            await poll_sweep(rig.client, [row], h.clock())
            await h.worker().run_once()
        replay = await rig.client.submit(row.to_delivery())  # even an explicit replay
    finally:
        await rig.aclose()
    assert (row.state, row.oan_status, row.oan_final) == ("accepted", "unknown", False)
    assert (replay.replayed, replay.status, replay.id) == (True, "unknown", row.oan_message_id)
    assert rig.transport.count("POST") == 2  # the first submit and the explicit replay
    assert len(h.sms.calls) == 1 and len(await db_messages(h)) == 1


# ------------------------------------------------- 9. Telegram sent is not delivery


async def test_telegram_sent_is_reported_as_sent_not_delivered(h: Harness) -> None:
    token = await h.caller("grievance", bot="grievance")
    rig = make_rig(h, token)
    d = TelegramDelivery(
        idempotency_key=delivery_key("grievance", "G-1042-u3", "user", "telegram"),
        chat_id=812345670,
        text="Your grievance G-1042 was updated.",
        source_reference="grievance:G-1042:status_changed",
        purpose="transactional",
        expires_at=None,
        first_submitted_at=h.clock(),
    )
    try:
        sub = await rig.client.submit(d)
        h.clock.advance(seconds=5)
        await h.worker().run_once()
        for _ in range(3):
            h.clock.advance(days=3)
            await h.worker().run_once()
        view = await rig.client.get_status(sub.id)
    finally:
        await rig.aclose()
    assert len(h.tg.calls) == 1
    assert (view.status, view.final, view.delivery_receipts_supported) == ("sent", False, False)
    assert "delivered_at" not in view.raw
    assert "Telegram has no delivery receipts" in view.meaning


# ------------------------------- 10. separator-safe keys and unchanged references


async def test_separator_containing_inputs_give_distinct_keys_and_messages(
    h: Harness,
) -> None:
    token = await h.caller("da", bot=None)
    rig = make_rig(h, token)
    k1 = delivery_key("da", "visit:7", "farmer", "sms")
    k2 = delivery_key("da", "visit", "7:farmer", "sms")
    k3 = delivery_key("da:visit", "7", "farmer", "sms")
    assert len({k1, k2, k3}) == 3
    base = outbox_row(h, n=231, source_reference="da:visit:7:planned").to_delivery()
    try:
        subs = [
            await rig.client.submit(SmsDelivery(**{**vars(base), "idempotency_key": k}))
            for k in (k1, k2, k3)
        ]
        again = await rig.client.submit(SmsDelivery(**{**vars(base), "idempotency_key": k1}))
    finally:
        await rig.aclose()
    assert len({s.id for s in subs}) == 3 and not any(s.replayed for s in subs)
    assert (again.id, again.replayed) == (subs[0].id, True)
    assert len(await db_messages(h)) == 3


async def test_grievance_event_underscores_pass_through_unchanged(h: Harness) -> None:
    import grievance_notifications as gn

    token = await h.caller("grievance", bot=None)
    rig = make_rig(h, token)
    events = ["deadline_extended", "officer_assigned", "acknowledged"]
    ids: dict[str, str] = {}
    try:
        for i, name in enumerate(events):
            ev = gn.GrievanceEvent(
                delivery_id=str(uuid.uuid4()),
                event=name,
                grievance_id="G-1042",
                farmer_phone=fake_number(240 + i),
                message_text="Grievance G-1042 update.",
            )
            sub = await gn.notify(rig.client, ev, h.clock())
            assert sub.source_reference == f"grievance:G-1042:{name}"
            ids[name] = sub.id
        views = {n: await rig.client.get_status(mid) for n, mid in ids.items()}
    finally:
        await rig.aclose()
    for name, view in views.items():
        db = (await h.one("SELECT source_reference FROM messages WHERE id = :i", i=ids[name]))[0]
        assert view.source_reference == db == f"grievance:G-1042:{name}"
