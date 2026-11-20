"""Dispatch behaviour of the composed service: OTP expiry at dispatch and after an outage,
the shared rate limit across two workers with the real adapters, OTP priority, broadcast
starvation, channel/adapter mismatch between workers, and worker-loop robustness.

Tests named ``test_defect_*`` are failing regression tests for findings in
docs/test-report.md. ``test_finding_*`` tests pass and characterize a documented hazard."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
from typing import Any

import httpx
import pytest
from itsupport import Harness, fake_number, new_key
from sqlalchemy.exc import OperationalError

from oan_messaging.contracts import Purpose
from oan_messaging.worker import RateLimit

pytestmark = pytest.mark.postgres


# ------------------------------------------------------------------------- expiry


async def test_otp_expired_during_outage_is_never_sent_and_replay_stays_expired(
    h: Harness,
) -> None:
    token = await h.caller("esignet", bot=None)
    key = new_key()
    mid = (await h.post_sms(token, key)).json()["id"]  # expires T0 + 5 min
    h.clock.advance(minutes=6)  # workers were down
    await h.worker().run_once()
    assert h.sms.calls == []
    view = await h.status(token, mid)
    assert (view["status"], view["failure_code"], view["final"], view["attempts"]) == (
        "expired",
        "expired_before_send",
        True,
        0,
    )
    r = await h.post_sms(token, key, expires_at=view["expires_at"])
    assert r.status_code == 200 and r.json()["status"] == "expired"
    await h.worker().run_once()
    assert h.sms.calls == [] and (await h.one("SELECT count(*) FROM message_content"))[0] == 0


async def test_telegram_retry_after_beyond_otp_expiry_expires_without_second_call(
    h: Harness,
) -> None:
    h.tg.script = lambda b, n: httpx.Response(
        429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 600}}
    )
    token = await h.caller("tg-otp", bot="grievance")
    mid = (
        await h.post_tg(
            token,
            new_key(),
            purpose="otp",
            expires_at=(h.clock() + timedelta(minutes=5)).isoformat(),
        )
    ).json()["id"]
    await h.worker().run_once()
    h.clock.advance(minutes=11)
    await h.worker().run_once()
    view = await h.status(token, mid)
    assert (view["status"], view["failure_code"]) == ("expired", "expired_before_send")
    assert len(h.tg.calls) == 1


async def test_otp_expiring_while_waiting_for_a_rate_slot_is_not_sent(h: Harness) -> None:
    token = await h.caller("esignet", bot=None)
    first = (await h.post_sms(token, new_key(), recipient=fake_number(110))).json()["id"]
    h.clock.advance(milliseconds=1)  # equal priority: created_at decides the claim order
    second = (
        await h.post_sms(
            token,
            new_key(),
            recipient=fake_number(111),
            expires_at=(h.clock() + timedelta(milliseconds=600)).isoformat(),
        )
    ).json()["id"]
    await h.worker(sms_limit=RateLimit(requests=1, window=timedelta(seconds=1))).run_once()
    assert [c.body["phonenums"][0] for c in h.sms.calls] == [fake_number(110)]
    assert (await h.status(token, first))["status"] == "sent"
    assert (await h.status(token, second))["status"] == "expired"


# --------------------------------------------------------- rate limit and priority


async def test_two_workers_share_the_gateway_rate_limit_with_real_adapters(h: Harness) -> None:
    token = await h.caller("advisory", bot=None)
    for i in range(30):
        await h.post_sms(
            token,
            new_key(),
            recipient=fake_number(300 + i),
            purpose="broadcast",
            expires_at=None,
            text=f"Advisory {i}",
        )
    limit = RateLimit(requests=5, window=timedelta(seconds=1))
    w1 = h.worker("w1", sms_limit=limit, batch_size=10)
    w2 = h.worker("w2", sms_limit=limit, batch_size=10)
    for _ in range(200):
        await asyncio.gather(w1.run_once(), w2.run_once())
        if len(h.sms.calls) >= 30:
            break
        h.clock.advance(milliseconds=200)
    numbers = Counter(c.body["phonenums"][0] for c in h.sms.calls)
    assert len(numbers) == 30 and set(numbers.values()) == {1}  # all sent, each once
    times = sorted(c.at for c in h.sms.calls)
    worst = max(sum(1 for t in times if s <= t < s + timedelta(seconds=1)) for s in times)
    assert worst <= 5, f"{worst} gateway calls inside one 1 s window"


async def test_otp_overtakes_an_existing_broadcast_backlog(h: Harness) -> None:
    advisory = await h.caller("advisory", bot=None)
    esignet = await h.caller("esignet", bot=None)
    for i in range(40):
        await h.post_sms(
            advisory,
            new_key(),
            recipient=fake_number(400 + i),
            purpose="broadcast",
            expires_at=None,
            text=f"Advisory {i}",
        )
    h.clock.advance(seconds=1)
    await h.post_sms(esignet, new_key(), recipient=fake_number(120))
    await h.worker(batch_size=5).run_once()
    assert h.sms.calls[0].body["phonenums"] == [fake_number(120)]


async def test_sustained_higher_priority_load_does_not_starve_broadcast(h: Harness) -> None:
    """SMS-03 broadcast ageing: transactional traffic arriving at gateway capacity can delay
    an older broadcast but not starve it. Once it has waited ``broadcast_max_wait`` (180 s)
    it is claimed ahead of the newer transactional messages, and the transactional class
    keeps up apart from that one slot."""
    advisory = await h.caller("advisory", bot=None)
    grievance = await h.caller("grievance", bot=None)
    t0 = h.clock()
    b = (
        await h.post_sms(
            advisory,
            new_key(),
            recipient=fake_number(500),
            purpose="broadcast",
            expires_at=None,
            text="Weekly advisory.",
        )
    ).json()["id"]
    worker = h.worker(sms_limit=RateLimit(requests=4, window=timedelta(seconds=1)), batch_size=4)
    n, sent_at = 0, None
    for _second in range(200):
        for _ in range(4):  # 4 new transactional messages per second == capacity
            n += 1
            await h.post_sms(
                grievance,
                new_key(),
                recipient=fake_number(600 + n % 300),
                purpose="transactional",
                expires_at=None,
                text=f"Update {n}",
            )
        claimed_at = h.clock()  # the limiter's waits also move the shared simulated clock
        await worker.run_once()
        if sent_at is None and (await h.status(advisory, b))["status"] == "sent":
            sent_at = (claimed_at - t0).total_seconds()
        h.clock.advance(seconds=1)
    # Delayed while younger than 180 s, then claimed in the first cycle after it.
    assert sent_at is not None and 180 <= sent_at < 183, sent_at
    sent_transactional = sum(1 for c in h.sms.calls if c.body["smstext"].startswith("Update"))
    assert sent_transactional >= 4 * 200 - 4  # the higher class lost about one slot


async def test_without_ageing_strict_priority_still_holds(h: Harness) -> None:
    """broadcast_max_wait=None keeps the old strict order (priority, created_at)."""
    advisory = await h.caller("advisory", bot=None)
    grievance = await h.caller("grievance", bot=None)
    b = (
        await h.post_sms(advisory, new_key(), recipient=fake_number(501), purpose="broadcast",
                         expires_at=None, text="Advisory.")
    ).json()["id"]
    h.clock.advance(seconds=600)
    await h.post_sms(grievance, new_key(), recipient=fake_number(601), purpose="transactional",
                     expires_at=None, text="Update 1")
    await h.worker(batch_size=1, broadcast_max_wait=None).run_once()
    assert [c.body["smstext"] for c in h.sms.calls] == ["Update 1"]
    assert (await h.status(advisory, b))["status"] == "accepted"


async def test_aged_order_otp_first_then_oldest_waiting_then_priority(h: Harness) -> None:
    """With ageing: OTPs first; then messages waiting past broadcast_max_wait, oldest first
    whatever their purpose (an older alert keeps its place ahead of an aged broadcast);
    then the rest by priority."""
    esignet = await h.caller("esignet", bot=None)
    advisory = await h.caller("advisory", bot=None)
    grievance = await h.caller("grievance", bot=None)

    async def sms(token, purpose, text, n):
        otp_expiry = (h.clock() + timedelta(minutes=5)).isoformat()
        await h.post_sms(token, new_key(), recipient=fake_number(n), purpose=purpose,
                         expires_at=otp_expiry if purpose == "otp" else None, text=text)

    await sms(advisory, "alert", "old alert", 701)
    h.clock.advance(seconds=10)
    await sms(advisory, "broadcast", "aged broadcast", 702)
    h.clock.advance(seconds=200)
    await sms(grievance, "transactional", "new transactional", 703)
    await sms(advisory, "alert", "new alert", 704)
    await sms(advisory, "broadcast", "new broadcast", 705)
    await sms(esignet, "otp", "Your code is 482913.", 706)
    await h.worker(batch_size=10).run_once()
    assert [c.body["smstext"] for c in h.sms.calls] == [
        "Your code is 482913.",
        "old alert",
        "aged broadcast",
        "new transactional",
        "new alert",
        "new broadcast",
    ]


async def test_worker_without_an_adapter_does_not_claim_other_channel(
    h: Harness,
) -> None:
    """Channel-scoped claims keep a Telegram OTP available to its capable worker."""
    token = await h.caller("tg-otp", bot="grievance")
    mid = (
        await h.post_tg(
            token,
            new_key(),
            purpose="otp",
            expires_at=(h.clock() + timedelta(seconds=90)).isoformat(),
        )
    ).json()["id"]
    sms_only = h.worker("sms-only", tg=False)
    tg_only = h.worker("tg-only", sms=False)
    assert (await sms_only.run_once()).claimed == 0
    assert (await tg_only.run_once()).claimed == 1
    view = await h.status(token, mid)
    assert (view["status"], view["attempts"], len(h.tg.calls)) == ("sent", 1, 1)



# ------------------------------------------------------------- worker robustness


async def test_cancelled_worker_mid_provider_call_becomes_unknown_never_resent(
    h: Harness,
) -> None:
    """SIMULATED interruption: task cancellation while the 8028 request is in flight (as
    a graceful shutdown would do). Genuine SIGKILL is in test_int_process.py."""
    reached = asyncio.Event()

    async def hang(_b: dict[str, Any], _n: int) -> httpx.Response:
        reached.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    h.sms.script = hang
    token = await h.caller("auth", bot=None)
    mid = (await h.post_sms(token, new_key())).json()["id"]
    task = asyncio.create_task(h.worker().run_once())
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await h.status(token, mid))["status"] == "sending"
    h.sms.script = None
    h.clock.advance(minutes=3)  # past the lease
    await h.worker("w2").run_once()
    view = await h.status(token, mid)
    assert (view["status"], view["failure_code"], view["provider_status"]) == (
        "unknown",
        "interrupted",
        "interrupted",
    )
    assert len(h.sms.calls) == 1


async def test_defect_worker_loop_dies_on_a_transient_storage_error(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIMULATED transient database error (one OperationalError from recover_stale, as a
    PostgreSQL restart or failover produces). ``Worker.run_forever`` (dispatcher.py:142-149)
    has no error handling, so the delivery loop ends and the queue stops draining until
    something restarts the process. ``cleanup_forever`` already survives the same error
    (cleanup.py:72-76).

    Status (MSG-07 takeover): this regression assumes IN-PROCESS recovery. Supervised
    process restart is an equally valid MSG-05/08 design; its recovery path is shown by
    test_finding_restarted_worker_resumes_after_the_loop_exits. Assertion unchanged."""
    token = await h.caller("esignet", bot=None)
    worker = h.worker()
    real = h.repo.recover_stale
    failures = {"left": 1}

    async def flaky(now: Any) -> int:
        if failures["left"]:
            failures["left"] -= 1
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        return await real(now)

    monkeypatch.setattr(h.repo, "recover_stale", flaky)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop, idle=0.01))
    await asyncio.sleep(0.05)
    await h.post_sms(token, new_key())
    for _ in range(100):
        if h.sms.calls or task.done():
            break
        await asyncio.sleep(0.02)
    stop.set()
    alive = not task.done()
    if task.done():
        task.exception()  # retrieve it so asyncio does not warn
    else:
        await asyncio.wait_for(task, 5)
    assert alive and len(h.sms.calls) == 1, (
        "worker.run_forever terminated on one transient storage error; the accepted "
        "OTP was never dispatched"
    )


async def test_purposes_map_to_documented_priorities(h: Harness) -> None:
    token = await h.caller("all", bot=None)
    order = ["broadcast", "alert", "transactional", "otp"]
    for i, purpose in enumerate(order):
        await h.post_sms(
            token,
            new_key(),
            recipient=fake_number(130 + i),
            purpose=purpose,
            expires_at=(h.clock() + timedelta(minutes=10)).isoformat()
            if purpose == Purpose.OTP.value
            else None,
        )
        h.clock.advance(seconds=1)
    await h.worker(batch_size=10).run_once()
    assert [c.body["phonenums"][0] for c in h.sms.calls] == [
        fake_number(133),
        fake_number(132),
        fake_number(131),
        fake_number(130),
    ]


# ------------------------------------------- MSG-07 takeover: findings A, B, D evidence


async def test_finding_broadcast_backlog_drains_once_load_drops_below_capacity(
    h: Harness,
) -> None:
    """Finding A characterization (passes). Strict priority defers broadcasts only while
    higher purposes saturate the gateway: the broadcast is not lost, and is sent in the
    first cycle with spare capacity. The defect is the missing BOUND during sustained
    saturation (test_defect_sustained_higher_priority_load_starves_broadcast_indefinitely)."""
    advisory = await h.caller("advisory", bot=None)
    grievance = await h.caller("grievance", bot=None)
    b = (
        await h.post_sms(
            advisory,
            new_key(),
            recipient=fake_number(500),
            purpose="broadcast",
            expires_at=None,
            text="Weekly advisory.",
        )
    ).json()["id"]
    worker = h.worker(sms_limit=RateLimit(requests=4, window=timedelta(seconds=1)), batch_size=4)
    n = 0
    for _second in range(20):  # 20 s at exactly gateway capacity
        for _ in range(4):
            n += 1
            await h.post_sms(
                grievance,
                new_key(),
                recipient=fake_number(600 + n),
                purpose="transactional",
                expires_at=None,
                text=f"Update {n}",
            )
        await worker.run_once()
        h.clock.advance(seconds=1)
        assert (await h.status(advisory, b))["status"] == "accepted"  # deferred meanwhile
    cycles = 0
    while (await h.status(advisory, b))["status"] == "accepted":
        cycles += 1
        assert cycles <= 3, "broadcast not sent after load stopped"
        await worker.run_once()
        h.clock.advance(seconds=1)
    assert (await h.status(advisory, b))["status"] == "sent"
    assert sum(1 for c in h.sms.calls if c.body["smstext"] == "Weekly advisory.") == 1


async def test_finding_restarted_worker_resumes_after_the_loop_exits(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding B characterization (passes). After run_forever exits on a SIMULATED transient
    storage error, a NEW worker (what a process supervisor would start) dispatches the
    queued message exactly once. State stays recoverable; what is missing is either
    in-process retry or a supervisor contract, plus a log line before exit."""
    token = await h.caller("esignet", bot=None)
    worker = h.worker("w-old")
    real = h.repo.recover_stale
    failures = {"left": 1}

    async def flaky(now: Any) -> int:
        if failures["left"]:
            failures["left"] -= 1
            raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        return await real(now)

    monkeypatch.setattr(h.repo, "recover_stale", flaky)
    await h.post_sms(token, new_key())
    with pytest.raises(OperationalError):
        await worker.run_once()  # direct cycles expose errors; the loop retries them
    assert h.sms.calls == []
    await h.worker("w-restarted").run_once()  # supervised restart
    assert len(h.sms.calls) == 1


async def test_finding_workers_with_every_adapter_never_strand_another_channel(
    h: Harness,
) -> None:
    """Finding D characterization (passes). The hazard in
    test_worker_without_an_adapter_does_not_claim_other_channel disappears when
    every worker is configured with every enabled channel's adapter: the same 90 s
    Telegram OTP is sent by whichever worker claims it first. This is the composition
    policy MSG-08 must either enforce at start-up or replace with channel-scoped claims."""
    token = await h.caller("tg-otp", bot="grievance")
    mid = (
        await h.post_tg(
            token,
            new_key(),
            purpose="otp",
            expires_at=(h.clock() + timedelta(seconds=90)).isoformat(),
        )
    ).json()["id"]
    first, second = h.worker("all-1"), h.worker("all-2")
    assert (await first.run_once()).claimed == 1
    await second.run_once()
    view = await h.status(token, mid)
    assert (view["status"], view["attempts"], len(h.tg.calls)) == ("sent", 1, 1)
