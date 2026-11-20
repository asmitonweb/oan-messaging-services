"""MSG-06 correction 4: owning-service outbox pattern. Durable delivery identity,
post-commit dispatch, OAN message-id storage and status reconciliation. A 202 is recorded
as accepted, never as sent or delivered. MOCKED HTTP and the in-memory API store only."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from email.utils import format_datetime

import httpx
from conftest import T0, ApiStore, FakeClock, client_for
from oan_client import OanClient
from outbox_dispatch import OutboxRow, dispatch, is_due, poll_sweep, reconcile
from test_client import MID, accepted, problem, sequence, status_body

from oan_messaging.contracts import MessageStatus

DELIVERY_ID = "c3d4e5f6-a7b8-4c9d-8e0f-1a2b3c4d5e6f"
OTHER = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
REDIRECT = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a"


def row(**kw: object) -> OutboxRow:
    base: dict[str, object] = {
        "delivery_id": DELIVERY_ID, "recipient": "0900000003",
        "text": "Grievance G-1042 received.", "source_reference": "grievance:G-1042:acknowledged",
        "purpose": "transactional", "expires_at": None, "first_submitted_at": T0}
    base.update(kw)
    return OutboxRow(**base)  # type: ignore[arg-type]


async def test_202_is_recorded_as_accepted_with_the_oan_id_not_as_sent(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(accepted(202)), clock)
    r = row()
    await dispatch(c, r, T0)
    assert (r.state, r.oan_message_id, r.oan_status) == ("accepted", MID, "accepted")
    assert r.oan_final is False and rec.requests[0].headers["idempotency-key"] == DELIVERY_ID
    await c.aclose()


async def test_uncertain_stays_queued_and_the_sweep_resends_identical_bytes(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(*[problem(503, "service_unavailable")] * 4, accepted(200)),
                        clock)
    r = row()
    await dispatch(c, r, T0)
    assert r.state == "queued" and r.attempts == 1
    assert r.next_attempt_at == T0 + timedelta(minutes=5)
    await dispatch(c, r, T0 + timedelta(minutes=1))  # not due yet: nothing sent
    assert len(rec.requests) == 4
    await dispatch(c, r, T0 + timedelta(minutes=5))
    assert r.state == "accepted" and r.oan_message_id == MID
    assert len({req.content for req in rec.requests}) == 1
    assert {req.headers["idempotency-key"] for req in rec.requests} == {DELIVERY_ID}
    await c.aclose()


async def test_long_retry_after_reschedules_no_earlier_than_the_server_allows(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(httpx.Response(429, headers={"Retry-After": "9999"})), clock)
    r = row()
    await dispatch(c, r, T0)
    assert r.state == "queued" and r.next_attempt_at == T0 + timedelta(seconds=9999)
    assert not is_due(r, T0 + timedelta(seconds=9998)) and is_due(r, r.next_attempt_at)
    assert len(rec.requests) == 1 and clock.slept == []
    await c.aclose()


async def test_definite_rejection_and_conflict_are_terminal_and_safe(clock: FakeClock) -> None:
    c, _ = client_for(sequence(problem(422, "validation_failed",
                                       [{"field": "text", "code": "too_long_for_single_sms"}]),
                               problem(409, "idempotency_conflict")), clock)
    r1, r2 = row(), row()
    await dispatch(c, r1, T0)
    await dispatch(c, r2, T0)
    assert (r1.state, r1.error_code) == ("rejected", "422:validation_failed")
    assert (r2.state, r2.error_code) == ("conflict", "idempotency_conflict")
    assert not is_due(r1, T0 + timedelta(days=1)) and not is_due(r2, T0 + timedelta(days=1))
    await c.aclose()


async def test_guarantee_window_moves_the_row_to_reconciliation(clock: FakeClock) -> None:
    c, rec = client_for(sequence(accepted(202)), clock)
    r = row(first_submitted_at=T0 - timedelta(days=30))
    await dispatch(c, r, T0)
    assert r.state == "reconcile" and rec.requests == []
    await c.aclose()


async def test_reconcile_polls_status_into_a_separate_field_until_final(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(accepted(202),
                                 httpx.Response(200, json=status_body("sent")),
                                 httpx.Response(200, json=status_body(
                                     "delivered", final=True,
                                     delivered_at="2026-09-01T05:12:19Z"))), clock)
    r = row()
    await dispatch(c, r, T0)
    await reconcile(c, r, T0)
    assert (r.state, r.oan_status, r.oan_final) == ("accepted", "sent", False)
    assert r.next_poll_at == T0 + timedelta(minutes=1)  # bounded fallback backoff
    await reconcile(c, r, T0 + timedelta(seconds=30))  # not due: no GET
    await reconcile(c, r, T0 + timedelta(minutes=1))
    assert (r.oan_status, r.oan_final) == ("delivered", True)
    assert r.next_poll_at is None
    await reconcile(c, r, T0 + timedelta(days=365))  # final: never polled again
    assert [q.method for q in rec.requests] == ["POST", "GET", "GET"]
    await c.aclose()


async def test_unknown_status_is_never_resubmitted(
    api_client: OanClient, api_store: ApiStore,
) -> None:
    r = row(delivery_id=str(uuid.uuid4()))
    await dispatch(api_client, r, T0)
    assert r.oan_message_id is not None
    mid = uuid.UUID(r.oan_message_id)
    api_store.set_status(mid, status=MessageStatus.SENDING, attempts=1, last_attempt_at=T0)
    api_store.set_status(mid, status=MessageStatus.UNKNOWN, provider_status="timeout",
                         content_purged_at=T0)
    await reconcile(api_client, r, T0)
    await dispatch(api_client, r, T0 + timedelta(days=1))  # not queued: no-op
    assert r.oan_status == "unknown" and r.state == "accepted"
    assert len(api_store.rows) == 1


async def test_reconcile_of_an_unknown_id_goes_to_operators(clock: FakeClock) -> None:
    c, _ = client_for(sequence(problem(404, "not_found")), clock)
    r = row(state="accepted", oan_message_id=MID)
    await reconcile(c, r, T0)
    assert r.state == "reconcile" and r.error_code == "404:not_found" and r.needs_attention
    assert r.next_poll_at is None
    await c.aclose()


def test_outbox_row_repr_hides_text_and_recipient() -> None:
    shown = repr(row())
    assert "0900000003" not in shown and "G-1042 received" not in shown
    assert DELIVERY_ID not in shown


# ------------------------------------- v2 review: persisted, server-respecting polling


def accepted_row(**kw: object) -> OutboxRow:
    base: dict[str, object] = {"state": "accepted", "oan_message_id": MID,
                               "oan_status": "accepted"}
    base.update(kw)
    return row(**base)


def reload(r: OutboxRow) -> OutboxRow:
    """Simulate a restart: persist the row as JSON and load it back."""
    return OutboxRow.from_record(json.loads(json.dumps(r.to_record())))


def limited(status: int, value: str) -> httpx.Response:
    return httpx.Response(status, headers={"Retry-After": value})


async def test_reported_case_no_second_get_after_retry_after_9999(clock: FakeClock) -> None:
    c, rec = client_for(sequence(limited(429, "9999")), clock)
    r = accepted_row()
    await reconcile(c, r, T0)
    await reconcile(c, r, T0)  # the reviewer's repro: same fake time, again
    assert len(rec.requests) == 1 and clock.slept == []
    assert r.next_poll_at == T0 + timedelta(seconds=9999)
    assert r.next_attempt_at is None and r.state == "accepted"  # polling state only
    await c.aclose()


async def test_seconds_deadline_survives_reload_and_polling_resumes_at_it(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(limited(429, "9999"),
                                 httpx.Response(200, json=status_body("sent"))), clock)
    r = accepted_row()
    await reconcile(c, r, T0)
    deadline = T0 + timedelta(seconds=9999)
    r = reload(r)
    assert r.next_poll_at == deadline
    for early in (T0, T0 + timedelta(hours=1), deadline - timedelta(microseconds=1)):
        assert await poll_sweep(c, [r], early) == 0
    assert len(rec.requests) == 1  # no GET before the stored deadline
    assert await poll_sweep(c, [reload(r)], deadline) == 1
    assert [q.method for q in rec.requests] == ["GET", "GET"]
    await c.aclose()


async def test_http_date_deadline_survives_reload(clock: FakeClock) -> None:
    when = T0 + timedelta(days=2)
    c, rec = client_for(sequence(limited(503, format_datetime(when, usegmt=True)),
                                 httpx.Response(200, json=status_body("sent"))), clock)
    r = accepted_row()
    await reconcile(c, r, T0)
    r = reload(r)
    assert r.next_poll_at == when
    await reconcile(c, r, when - timedelta(seconds=1))
    assert len(rec.requests) == 1
    await reconcile(c, r, when)
    assert len(rec.requests) == 2 and r.oan_status == "sent"
    await c.aclose()


async def test_exhausted_retries_keep_the_server_deadline(clock: FakeClock) -> None:
    c, rec = client_for(sequence(limited(503, "5")), clock, max_attempts=1)
    r = accepted_row()
    await reconcile(c, r, T0, poll_base=timedelta(seconds=1))
    assert r.next_poll_at == T0 + timedelta(seconds=5)  # server's 5 s beats our 1 s
    await c.aclose()


async def test_fallback_polling_backoff_is_bounded_without_a_server_deadline(
    clock: FakeClock,
) -> None:
    c, rec = client_for(lambda _req: httpx.Response(200, json=status_body("sending")), clock)
    r = accepted_row()
    now, gaps = T0, []
    for _ in range(14):
        await reconcile(c, r, now)
        assert r.next_poll_at is not None
        gaps.append(r.next_poll_at - now)
        now = r.next_poll_at
    assert gaps[:4] == [timedelta(minutes=m) for m in (1, 2, 4, 8)]
    assert max(gaps) == timedelta(hours=6) and gaps[-1] == timedelta(hours=6)
    assert all(q.method == "GET" for q in rec.requests)
    await c.aclose()


async def test_transport_failures_back_off_instead_of_repolling(clock: FakeClock) -> None:
    c, rec = client_for(sequence(*[httpx.ReadTimeout("t")] * 4), clock)
    r = accepted_row()
    await reconcile(c, r, T0)
    await reconcile(c, r, T0)
    assert len(rec.requests) == 4 and r.next_poll_at == T0 + timedelta(minutes=1)
    await c.aclose()


async def test_malformed_status_is_recorded_per_row_and_the_sweep_continues(
    clock: FakeClock,
) -> None:
    responses = {
        MID: httpx.Response(200, json={"id": MID, "unexpected": True}),  # malformed
        OTHER: httpx.Response(200, json=status_body("delivered", final=True,
                                                    delivered_at="2026-09-01T05:12:19Z")),
        REDIRECT: httpx.Response(302, headers={"Location": "https://evil.invalid/"}),
    }
    c, rec = client_for(lambda req: responses[req.url.path.rsplit("/", 1)[-1]], clock)
    bad, good, moved = (accepted_row(), accepted_row(oan_message_id=OTHER),
                        accepted_row(oan_message_id=REDIRECT))
    corrupt = accepted_row(oan_message_id="not-a-uuid")  # bad stored id: no request at all
    assert await poll_sweep(c, [bad, corrupt, good, moved], T0) == 4
    assert (good.oan_status, good.oan_final) == ("delivered", True)
    for r in (bad, corrupt, moved):
        assert r.error_code == "status_protocol_error" and r.needs_attention
        assert r.state == "accepted" and r.next_poll_at == T0 + timedelta(minutes=1)
    assert [q.method for q in rec.requests] == ["GET", "GET", "GET"]  # never a POST
    assert await poll_sweep(c, [bad, corrupt, good, moved], T0) == 0  # not repolled at once
    await c.aclose()


async def test_polling_failures_never_requeue_for_submission(clock: FakeClock) -> None:
    c, rec = client_for(sequence(problem(503, "x"), problem(403, "forbidden"),
                                 httpx.Response(200, json=status_body("unknown"))), clock,
                        max_attempts=1)
    r = accepted_row()
    for t in (T0, T0 + timedelta(days=1), T0 + timedelta(days=2)):
        await reconcile(c, r, t, poll_base=timedelta(seconds=1))
        await dispatch(c, r, t)  # a sweep running dispatch too: must not POST
        assert r.state == "accepted"
    assert r.oan_status == "unknown" and r.oan_final is False
    assert all(q.method == "GET" for q in rec.requests)
    await c.aclose()


async def test_final_rows_are_never_polled(clock: FakeClock) -> None:
    c, rec = client_for(sequence(), clock)
    rows = [accepted_row(oan_final=True, oan_status=s) for s in ("delivered", "failed")]
    rows += [row(), row(state="rejected"), row(state="reconcile", oan_message_id=MID)]
    assert await poll_sweep(c, rows, T0 + timedelta(days=400)) == 0
    assert rec.requests == []
    await c.aclose()


async def test_ambiguous_submission_protocol_error_is_retried_same_key_and_signalled(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(httpx.Response(202, json={"unexpected": True}),
                                 accepted(200)), clock)
    r = row()
    await dispatch(c, r, T0)
    assert (r.state, r.error_code, r.needs_attention) == ("queued", "protocol_error", True)
    assert r.next_attempt_at == T0 + timedelta(minutes=5)
    await dispatch(c, r, T0 + timedelta(minutes=1))  # bounded schedule: not yet
    await dispatch(c, r, T0 + timedelta(minutes=5))
    assert r.state == "accepted" and r.oan_message_id == MID
    assert {q.headers["idempotency-key"] for q in rec.requests} == {DELIVERY_ID}
    assert len({q.content for q in rec.requests}) == 1
    await c.aclose()


def test_round_trip_preserves_every_field() -> None:
    r = accepted_row(next_poll_at=T0 + timedelta(seconds=9999), polls=3,
                     expires_at=T0 + timedelta(days=1), needs_attention=True)
    assert reload(r).to_record() == r.to_record()


async def test_sent_sms_polling_deadline_survives_restart(clock: FakeClock) -> None:
    c, calls = client_for(sequence(httpx.Response(200, json=status_body("sent", final=False,
        sent_at=T0.isoformat()))), clock)
    r = accepted_row()
    await reconcile(c, r, T0, receipt_wait=timedelta(hours=24))
    r = OutboxRow.from_record(r.to_record())
    assert r.poll_deadline == T0 + timedelta(hours=24)
    await reconcile(c, r, r.poll_deadline)
    assert r.poll_complete and not r.oan_final
    assert r.needs_attention and r.error_code == "receipt_wait_elapsed"
    assert len(calls.requests) == 1
    await c.aclose()


async def test_unknown_stops_polling_and_requires_attention(clock: FakeClock) -> None:
    c, calls = client_for(
        sequence(httpx.Response(200, json=status_body("unknown", final=False))), clock
    )
    r = accepted_row()
    await reconcile(c, r, T0)
    await reconcile(c, r, T0 + timedelta(days=100))
    assert r.poll_complete and not r.oan_final and r.needs_attention
    assert len(calls.requests) == 1
    await c.aclose()


def test_da_long_horizon_caps_deadline_once() -> None:
    from da_visits import VisitEvent, VisitRecipient, to_deliveries
    event = VisitEvent("event-long", "visit-long", "planned",
        (VisitRecipient("farmer", "0900000003", "Visit planned."),), T0 + timedelta(days=40))
    delivery = to_deliveries(event, T0)[0]
    assert delivery.expires_at == T0 + timedelta(days=7, minutes=-5)
    assert to_deliveries(event, T0)[0] == delivery
