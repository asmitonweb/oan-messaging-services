"""MSG-06 correction 1: a server Retry-After is not cut to the local budget. Within the
budget it is waited out exactly; beyond it the call stops with a structured
retry-not-before result, and the stored delivery is retried unchanged no earlier. Only
values above 10 years are clamped (documented on MAX_RETRY_AFTER_SECONDS).
MOCKED HTTP only."""

from __future__ import annotations

import json
from datetime import timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from conftest import T0, FakeClock, client_for, config
from oan_client import OanRetryDeferred, OanUncertain, ReconciliationRequired
from oan_client.client import MAX_RETRY_AFTER_SECONDS
from test_client import KEY, MID, accepted, otp, sequence, status_body


def limited(status: int = 429, retry_after: str = "9999") -> httpx.Response:
    return httpx.Response(status, headers=[(b"Retry-After", retry_after.encode("utf-8"))])


# ------------------------------------------------------------------ submission path


@pytest.mark.parametrize("status", [429, 503])
async def test_long_retry_after_defers_without_an_early_retry(clock: FakeClock,
                                                              status: int) -> None:
    c, rec = client_for(sequence(limited(status, "9999"), accepted(202)), clock)
    with pytest.raises(OanRetryDeferred) as info:
        await c.submit(otp())
    exc = info.value
    assert len(rec.requests) == 1 and clock.slept == []  # nothing retried, nothing slept
    assert exc.retry_not_before == T0 + timedelta(seconds=9999)
    assert exc.idempotency_key == KEY and exc.attempts == 1 and exc.last == f"http_{status}"
    assert isinstance(exc, OanUncertain)  # existing "keep it queued" handling still applies
    await c.aclose()


async def test_http_date_retry_after_beyond_budget_defers_to_that_date(
    clock: FakeClock,
) -> None:
    when = T0 + timedelta(hours=2)
    c, rec = client_for(sequence(limited(503, format_datetime(when, usegmt=True))), clock)
    with pytest.raises(OanRetryDeferred) as info:
        await c.submit(otp())
    assert info.value.retry_not_before == when and len(rec.requests) == 1
    await c.aclose()


async def test_http_date_retry_after_within_budget_is_waited_exactly(clock: FakeClock) -> None:
    when = T0 + timedelta(seconds=12)
    c, _ = client_for(sequence(limited(429, format_datetime(when, usegmt=True)),
                               accepted(202)), clock)
    await c.submit(otp())
    assert clock.slept == [12.0]
    await c.aclose()


async def test_budget_boundary_is_inclusive(clock: FakeClock) -> None:
    c, _ = client_for(sequence(limited(429, "30"), accepted(202)), clock)
    await c.submit(otp())
    assert clock.slept == [30.0]  # exactly the budget: waited, not deferred
    c2, rec2 = client_for(sequence(limited(429, "31")), clock)
    with pytest.raises(OanRetryDeferred):
        await c2.submit(otp())
    assert len(rec2.requests) == 1
    await c.aclose()
    await c2.aclose()


async def test_configurable_budget_is_honoured(clock: FakeClock) -> None:
    c, rec = client_for(sequence(limited(429, "300"), accepted(202)), clock,
                        max_retry_after=600.0)
    await c.submit(otp())
    assert clock.slept == [300.0] and len(rec.requests) == 2
    await c.aclose()


async def test_retry_after_on_the_last_attempt_still_carries_retry_not_before(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(limited(503, "5")), clock, max_attempts=1)
    with pytest.raises(OanUncertain) as info:
        await c.submit(otp())
    assert not isinstance(info.value, OanRetryDeferred)
    assert info.value.retry_not_before == T0 + timedelta(seconds=5)
    assert len(rec.requests) == 1 and clock.slept == []
    await c.aclose()


async def test_deferred_retry_resends_identical_key_bytes_expiry_and_window(
    clock: FakeClock,
) -> None:
    d = otp()
    c, rec = client_for(sequence(limited(429, "9999"), accepted(200)), clock)
    with pytest.raises(OanRetryDeferred) as info:
        await c.submit(d)
    assert info.value.retry_not_before is not None
    clock.now = info.value.retry_not_before  # the outbox reschedules no earlier
    sub = await c.submit(d)  # the SAME stored delivery
    assert sub.replayed and sub.id == MID
    first, second = rec.requests
    assert first.content == second.content
    assert first.headers["idempotency-key"] == second.headers["idempotency-key"] == KEY
    assert json.loads(second.content)["expires_at"] == "2026-09-01T05:17:13+00:00"
    assert d.first_submitted_at == T0  # never re-dated
    await c.aclose()


# ------------------------------------------------------- guarantee-window boundaries


async def test_retry_after_ending_at_the_window_requires_reconciliation(
    clock: FakeClock,
) -> None:
    d = otp(first_submitted_at=T0 - timedelta(days=30) + timedelta(seconds=10))
    c, rec = client_for(sequence(limited(429, "10")), clock)
    with pytest.raises(ReconciliationRequired) as info:
        await c.submit(d)
    assert info.value.retry_not_before == T0 + timedelta(seconds=10)
    assert len(rec.requests) == 1 and clock.slept == []
    await c.aclose()


async def test_retry_after_just_inside_the_window_is_waited(clock: FakeClock) -> None:
    d = otp(first_submitted_at=T0 - timedelta(days=30) + timedelta(seconds=11))
    c, rec = client_for(sequence(limited(429, "10"), accepted(200)), clock)
    assert (await c.submit(d)).replayed
    assert clock.slept == [10.0] and len(rec.requests) == 2
    await c.aclose()


async def test_long_retry_after_beyond_the_window_requires_reconciliation(
    clock: FakeClock,
) -> None:
    c, _ = client_for(sequence(limited(429, str(31 * 86400))), clock)
    with pytest.raises(ReconciliationRequired):
        await c.submit(otp())  # first_submitted_at == T0, window ends at T0 + 30 d
    await c.aclose()


# ---------------------------------------------------------------------- status path


async def test_status_long_retry_after_defers(clock: FakeClock) -> None:
    c, rec = client_for(sequence(limited(503, "9999")), clock)
    with pytest.raises(OanRetryDeferred) as info:
        await c.get_status(MID)
    assert info.value.retry_not_before == T0 + timedelta(seconds=9999)
    assert [r.method for r in rec.requests] == ["GET"] and clock.slept == []
    await c.aclose()


async def test_status_short_retry_after_is_waited_exactly(clock: FakeClock) -> None:
    c, rec = client_for(sequence(limited(429, "3"),
                                 httpx.Response(200, json=status_body("sent"))), clock)
    assert (await c.get_status(MID)).status == "sent"
    assert clock.slept == [3.0] and len(rec.requests) == 2
    await c.aclose()


async def test_status_http_date_beyond_budget_defers(clock: FakeClock) -> None:
    when = T0 + timedelta(days=40)  # no guarantee window applies to reads
    c, _ = client_for(sequence(limited(503, format_datetime(when, usegmt=True))), clock)
    with pytest.raises(OanRetryDeferred) as info:
        await c.get_status(MID)
    assert info.value.retry_not_before == when
    await c.aclose()


# ------------------------------------------------------------------ header parsing


@pytest.mark.parametrize("value", ["soon", "-5", "1.5", "²", "", "Wed, 99 Foo 2026 00:00:00",
                                   "Tue, 01 Sep 2026 05:12:13 -0000", "x" * 200])
async def test_malformed_retry_after_falls_back_to_normal_backoff(
    clock: FakeClock, value: str,
) -> None:
    c, rec = client_for(sequence(limited(503, value), accepted(202)), clock)
    await c.submit(otp())
    assert clock.slept == [0.5] and len(rec.requests) == 2
    await c.aclose()


async def test_past_http_date_allows_an_immediate_retry(clock: FakeClock) -> None:
    when = T0 - timedelta(minutes=5)
    c, _ = client_for(sequence(limited(503, format_datetime(when, usegmt=True)),
                               accepted(202)), clock)
    await c.submit(otp())
    assert clock.slept == [0.0]
    await c.aclose()


@pytest.mark.parametrize("value", ["9" * 13, "9" * 5000])
async def test_absurd_retry_after_is_clamped_to_ten_years_not_to_the_budget(
    clock: FakeClock, value: str,
) -> None:
    c, rec = client_for(sequence(limited(503, value)), clock)
    with pytest.raises(OanRetryDeferred) as info:
        await c.get_status(MID)
    assert info.value.retry_not_before == T0 + timedelta(seconds=MAX_RETRY_AFTER_SECONDS)
    assert len(rec.requests) == 1
    await c.aclose()


def test_deferred_result_is_safe_to_print(clock: FakeClock) -> None:
    exc = OanRetryDeferred(KEY, 1, "http_429", T0)
    shown = f"{exc} {exc!r}"
    assert KEY not in shown and KEY[-4:] in shown and T0.isoformat() in shown


@pytest.mark.parametrize("bad", [{"max_retry_after": 0}, {"max_retry_after": 3601},
                                 {"max_retry_after": -1}])
def test_wait_budget_is_bounded(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        config(**bad)


async def test_absurd_retry_after_on_a_submission_requires_reconciliation(
    clock: FakeClock,
) -> None:
    c, rec = client_for(sequence(limited(429, "9" * 5000)), clock)
    with pytest.raises(ReconciliationRequired):
        await c.submit(otp())  # clamped 10 years is still far beyond the 30-day window
    assert len(rec.requests) == 1 and clock.slept == []
    await c.aclose()
