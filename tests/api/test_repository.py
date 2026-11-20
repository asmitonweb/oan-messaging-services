"""Repository operations MSG-05 will drive: claims, leases, attempts, transitions, receipts
and cleanup. Real PostgreSQL; fake clock; no workers, providers or callback HTTP here."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from conftest import T0, Secrets, make_caller, make_repo, new_key, sms_body, tg_body
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import (
    DEFAULT_POLICY,
    CallerContext,
    Channel,
    DeliveryReceipt,
    FailureCode,
    HmacFingerprinter,
    LeaseLost,
    MessageStatus,
    Provider,
    ProviderOutcome,
    ProviderResult,
    Purpose,
    ReceiptDisposition,
    SmsSendRequest,
    SmsSubmission,
    StoredMessage,
    TelegramSendRequest,
    TelegramSubmission,
)
from oan_messaging.storage.credentials import authenticate, upsert_caller
from oan_messaging.storage.errors import KeyRetirementRefused, MessageExpired
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import message_attempts, message_content, messages, receipts

LEASE = timedelta(minutes=2)


async def caller(
    sf: async_sessionmaker[AsyncSession], keys: Secrets, cid: str = "svc", **kw: Any
) -> CallerContext:
    token = await make_caller(sf, keys, cid, **kw)
    ctx = await authenticate(sf, keys.pepper, token)
    assert ctx is not None
    return ctx


async def submit(
    repo: PostgresMessageRepository, ctx: CallerContext, *, at: Any = T0, **body: Any
) -> StoredMessage:
    sub = SmsSubmission.model_validate(sms_body(**body))
    msg, _ = await repo.create_or_get(ctx, new_key(), sub, at)
    return msg


async def row(sf: async_sessionmaker[AsyncSession], mid: Any) -> dict[str, Any]:
    async with sf() as s:
        return dict(
            (await s.execute(select(messages).where(messages.c.id == mid))).mappings().one()
        )


async def has_content(sf: async_sessionmaker[AsyncSession], mid: Any) -> bool:
    async with sf() as s:
        return (
            await s.execute(
                select(message_content.c.message_id).where(message_content.c.message_id == mid)
            )
        ).first() is not None


def res(outcome: ProviderOutcome, at: Any, **kw: Any) -> ProviderResult:
    data: dict[str, Any] = {
        "outcome": outcome,
        "provider": Provider.SMS_8028,
        "provider_status": kw.pop("status", "http_200"),
        "started_at": at,
        "finished_at": at,
    }
    if outcome is ProviderOutcome.SENT:
        data["provider_message_id"] = kw.pop("pid", "17006100")
    data.update(kw)
    return ProviderResult(**data)


def receipt(
    status: str, at: Any, pid: str = "17006100", phone: str = "+251900000001"
) -> DeliveryReceipt:
    return DeliveryReceipt(
        provider_message_id=pid, recipient=phone, raw_status=status, event_time=at, received_at=at
    )


# ----------------------------------------------------------------------- claims


async def test_claim_order_priority_and_decrypted_send_request(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    b = await submit(repo, ctx, purpose="broadcast", expires_at=None, recipient="0900000002")
    o = await submit(repo, ctx, at=T0 + timedelta(seconds=1))
    claims = await repo.claim_due("w1", T0 + timedelta(seconds=2), LEASE, 10)
    assert [c.message.id for c in claims] == [o.id, b.id]  # OTP first despite later creation
    req = claims[0].send_request
    assert isinstance(req, SmsSendRequest)
    assert req.recipient == "+251900000001" and req.text == "Your OAN code is 482913."
    assert req.attempt == 1 and req.expires_at == o.expires_at
    # Leased messages are not claimable by another worker until the lease expires.
    assert await repo.claim_due("w2", T0 + timedelta(seconds=3), LEASE, 10) == []


async def test_two_workers_claim_disjoint_sets_concurrently(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    ids = {
        (
            await submit(
                repo, ctx, purpose="broadcast", expires_at=None, recipient=f"09000001{i:02d}"
            )
        ).id
        for i in range(40)
    }
    other = make_repo(sf, keys)
    a, b = await asyncio.gather(
        repo.claim_due("w1", T0, LEASE, 25), other.claim_due("w2", T0, LEASE, 25)
    )
    got_a = {c.message.id for c in a}
    got_b = {c.message.id for c in b}
    assert not got_a & got_b and got_a | got_b == ids
    assert len(got_a) + len(got_b) == 40


async def test_expired_accepted_messages_expire_instead_of_being_claimed(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx)  # expires T0+5m
    assert await repo.claim_due("w1", T0 + timedelta(minutes=5), LEASE, 10) == []
    r = await row(sf, m.id)
    assert r["status"] == "expired" and r["failure_code"] == "expired_before_send"
    assert not await has_content(sf, m.id) and r["content_purged_at"] is not None


async def test_telegram_requires_currently_authorized_bot(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys, "grv", bot="grievance")
    sub = TelegramSubmission.model_validate(tg_body())
    m, _ = await repo.create_or_get(ctx, new_key(), sub, T0)
    # Caller loses its bot after acceptance: not claimable, never another bot, stays accepted.
    await upsert_caller(
        sf, "grv", channels=frozenset(Channel), purposes=frozenset(Purpose), telegram_bot=None
    )
    assert await repo.claim_due("w1", T0, LEASE, 10) == []
    assert (await row(sf, m.id))["status"] == "accepted"
    await upsert_caller(
        sf,
        "grv",
        channels=frozenset(Channel),
        purposes=frozenset(Purpose),
        telegram_bot="grievance",
    )
    (claim,) = await repo.claim_due("w1", T0, LEASE, 10)
    assert isinstance(claim.send_request, TelegramSendRequest)
    assert claim.send_request.bot == "grievance" and claim.send_request.chat_id == 555000111


# ---------------------------------------------------------- attempts and leases


async def test_attempt_lifecycle_sent_purges_content_and_records_attempt(  # A15
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx)
    (claim,) = await repo.claim_due("worker-7", T0, LEASE, 1)
    assert await repo.begin_attempt(claim, T0) == 1
    r = await row(sf, m.id)
    assert r["status"] == "sending" and r["last_attempt_at"] == T0 and r["attempts"] == 1
    assert await has_content(sf, m.id)  # still needed while sending
    done = await repo.finish_attempt(
        claim, res(ProviderOutcome.SENT, T0 + timedelta(seconds=1)), None
    )
    assert done.status is MessageStatus.SENT and done.provider_message_id == "17006100"
    assert done.content_purged_at is not None and not await has_content(sf, m.id)
    async with sf() as s:
        att = (await s.execute(select(message_attempts))).mappings().one()
    assert att["worker_id"] == "worker-7" and att["outcome"] == "sent"
    assert att["finished_at"] == T0 + timedelta(seconds=1)


async def test_recovered_attempt_accepts_fenced_success_without_resend(  # A16 + lease ownership
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    # Worker w1 hangs past its lease; recovery marks the attempt unknown and purges.
    assert await repo.recover_stale(T0 + LEASE) == 1
    r = await row(sf, m.id)
    assert r["status"] == "unknown" and r["failure_code"] == "interrupted"
    assert r["lease_token"] is None and not await has_content(sf, m.id)
    # Exact-attempt success supplies evidence lost during recovery, without resending.
    await repo.finish_attempt(claim, res(ProviderOutcome.SENT, T0 + LEASE), None)
    assert (await row(sf, m.id))["status"] == "sent"
    assert await repo.claim_due("w2", T0 + LEASE, LEASE, 10) == []  # never resent
    async with sf() as s:
        att = (await s.execute(select(message_attempts))).mappings().one()
    assert att["outcome"] == "sent" and att["failure_code"] is None


async def test_begin_attempt_rechecks_expiry_and_lease(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx)
    (claim,) = await repo.claim_due("w1", T0 + timedelta(minutes=4, seconds=30), LEASE, 1)
    with pytest.raises(MessageExpired):
        await repo.begin_attempt(claim, T0 + timedelta(minutes=5))
    expired = await repo.mark_expired(claim, T0 + timedelta(minutes=5))
    assert expired.status is MessageStatus.EXPIRED and not await has_content(sf, m.id)
    with pytest.raises(LeaseLost):
        await repo.begin_attempt(claim, T0 + timedelta(minutes=5))


async def test_abandoned_claim_becomes_claimable_after_lease(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (c1,) = await repo.claim_due("w1", T0, LEASE, 1)
    (c2,) = await repo.claim_due("w2", T0 + LEASE, LEASE, 1)
    assert c1.claim_token != c2.claim_token
    with pytest.raises(LeaseLost):
        await repo.begin_attempt(c1, T0 + LEASE)  # w1's lease was taken over


@pytest.mark.parametrize(
    ("kw", "expected_status", "failure"),
    [
        (
            {
                "retryable": True,
                "failure_code": FailureCode.PROVIDER_UNAVAILABLE,
                "status": "conn_refused",
            },
            "accepted",
            None,
        ),
        (
            {"retryable": False, "failure_code": FailureCode.PROVIDER_AUTH, "status": "http_401"},
            "failed",
            "provider_auth",
        ),
        (
            {
                "retryable": False,
                "failure_code": FailureCode.EXPIRED_BEFORE_SEND,
                "status": "expired",
            },
            "expired",
            "expired_before_send",
        ),
    ],
)
async def test_not_sent_outcomes(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    kw: dict[str, Any],
    expected_status: str,
    failure: str | None,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    out = await repo.finish_attempt(
        claim, res(ProviderOutcome.NOT_SENT, T0, **kw), T0 + timedelta(seconds=30)
    )
    assert out.status.value == expected_status
    assert (out.failure_code.value if out.failure_code else None) == failure
    assert await has_content(sf, m.id) is (expected_status == "accepted")
    if expected_status == "accepted":
        assert out.not_before == T0 + timedelta(seconds=30)
        assert await repo.claim_due("w2", T0 + timedelta(seconds=29), LEASE, 1) == []
        (again,) = await repo.claim_due("w2", T0 + timedelta(seconds=30), LEASE, 1)
        assert again.send_request.attempt == 2


async def test_retry_after_is_never_shortened_and_expiry_wins(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    long_lived = await submit(
        repo, ctx, purpose="alert", expires_at=(T0 + timedelta(hours=1)).isoformat()
    )
    otp = await submit(repo, ctx, recipient="0900000002")  # expires T0+5m
    claims = {c.message.id: c for c in await repo.claim_due("w1", T0, LEASE, 2)}
    for c in claims.values():
        await repo.begin_attempt(c, T0)
    rl = {
        "retryable": True,
        "failure_code": FailureCode.RATE_LIMITED,
        "status": "tg_429",
        "retry_after_seconds": 120,
    }
    a = await repo.finish_attempt(
        claims[long_lived.id], res(ProviderOutcome.NOT_SENT, T0, **rl), T0 + timedelta(seconds=5)
    )  # worker asked for less: ignored
    assert a.status is MessageStatus.ACCEPTED and a.not_before == T0 + timedelta(seconds=120)
    rl["retry_after_seconds"] = 300  # reaches the OTP's expiry exactly -> expire, never send
    b = await repo.finish_attempt(claims[otp.id], res(ProviderOutcome.NOT_SENT, T0, **rl), None)
    assert b.status is MessageStatus.EXPIRED and b.failure_code is FailureCode.EXPIRED_BEFORE_SEND


async def test_database_trigger_rejects_illegal_transitions(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    for target in ("sent", "delivered", "unknown", "failed"):
        async with sf() as s:
            with pytest.raises(Exception) as info:
                async with s.begin():
                    await s.execute(
                        update(messages)
                        .where(messages.c.id == m.id)
                        .values(
                            status=target,
                            sent_at=T0,
                            delivered_at=T0,
                            failure_code="timeout",
                            attempts=1,
                            last_attempt_at=T0,
                        )
                    )
            assert "illegal message status transition" in str(info.value)
    assert (await row(sf, m.id))["status"] == "accepted"


# ------------------------------------------------------------------- receipts


async def sent_message(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    *,
    pid: str = "17006100",
    recipient: str = "0900000001",
) -> StoredMessage:
    ctx = await caller(sf, keys, f"svc-{pid}")
    await submit(repo, ctx, purpose="broadcast", expires_at=None, recipient=recipient)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    return await repo.finish_attempt(claim, res(ProviderOutcome.SENT, T0, pid=pid), None)


async def test_a10_receipt_matched_after_purge_and_duplicates_out_of_order(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    m = await sent_message(sf, keys, repo)
    assert not await has_content(sf, m.id)
    t = T0 + timedelta(seconds=6)
    assert (
        await repo.record_receipt(receipt("Delivered", t, phone="+251900000001"), t)
        is ReceiptDisposition.APPLIED
    )
    r = await row(sf, m.id)
    assert r["status"] == "delivered" and r["delivered_at"] == t
    assert r["provider_status"] == "Delivered"
    # Duplicate and late "Sent" after "Delivered": recorded, no change.
    assert (
        await repo.record_receipt(receipt("Delivered", t), t)
        is ReceiptDisposition.RECORDED_NO_CHANGE
    )
    assert await repo.record_receipt(receipt("Sent", t), t) is ReceiptDisposition.RECORDED_NO_CHANGE
    assert (await row(sf, m.id))["status"] == "delivered"


async def test_gateway_expired_and_undocumented_statuses(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    m = await sent_message(sf, keys, repo)
    assert (
        await repo.record_receipt(receipt("Failed", T0), T0)
        is ReceiptDisposition.RECORDED_NO_CHANGE
    )
    assert (await row(sf, m.id))["status"] == "sent"
    assert await repo.record_receipt(receipt("Expired", T0), T0) is ReceiptDisposition.APPLIED
    r = await row(sf, m.id)
    assert (r["status"], r["failure_code"], r["provider_status"]) == (
        "failed",
        "provider_delivery_expired",
        "Expired",
    )


async def test_a11_wrong_number_receipt_is_mismatched_without_change(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    m = await sent_message(sf, keys, repo)
    d = await repo.record_receipt(receipt("Delivered", T0, phone="+251900000999"), T0)
    assert d is ReceiptDisposition.MISMATCHED
    assert (await row(sf, m.id))["status"] == "sent"


async def test_a12_orphan_receipt_rematched_before_purge(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    # Callback arrives before the send response is stored.
    early = T0 + timedelta(milliseconds=500)
    assert (
        await repo.record_receipt(receipt("Delivered", early, pid="555"), early)
        is ReceiptDisposition.ORPHANED
    )
    assert (
        await repo.record_receipt(
            receipt("Sent", early - timedelta(milliseconds=100), pid="555"), early
        )
        is ReceiptDisposition.ORPHANED
    )
    done = await repo.finish_attempt(
        claim, res(ProviderOutcome.SENT, T0 + timedelta(seconds=1), pid="555"), None
    )
    assert done.status is MessageStatus.DELIVERED  # Sent then Delivered applied in event order
    assert done.content_purged_at is not None and not await has_content(sf, m.id)
    async with sf() as s:
        disp = sorted(r["disposition"] for r in (await s.execute(select(receipts))).mappings())
    # Delivered applies; the early "Sent" is a no-op because the send response already
    # recorded sent (out-of-order evidence never moves status backwards).
    assert disp == ["applied", "recorded_no_change"]


async def test_a13_receipts_store_no_raw_phone_numbers(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    await sent_message(sf, keys, repo)
    await repo.record_receipt(receipt("Delivered", T0), T0)
    await repo.record_receipt(receipt("Delivered", T0, pid="999", phone="+251900000777"), T0)
    async with sf() as s:
        dump = repr([dict(r) for r in (await s.execute(select(receipts))).mappings()])
    for raw in (
        "+251900000001",
        "0900000001",
        "251900000001",
        "900000001",
        "+251900000777",
        "900000777",
    ):
        assert raw not in dump
    assert "+2519******01" in dump and "+2519******77" in dump


async def test_a19_receipt_deadline_for_unknown_attempt_without_sent_at(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    # Gateway returned a malformed body that still carried an id: unknown, no sent_at.
    unk = await repo.finish_attempt(
        claim, res(ProviderOutcome.UNKNOWN, T0, failure_code=FailureCode.MALFORMED_RESPONSE), None
    )
    assert unk.status is MessageStatus.UNKNOWN and unk.sent_at is None
    # Anchor = last_attempt_at (no sent_at): fingerprint kept until anchor + receipt_window.
    deadline = T0 + DEFAULT_POLICY.receipt_window
    await repo.purge_due(deadline - timedelta(seconds=1), 100)
    assert (await row(sf, m.id))["recipient_fp_digest"] is not None
    await repo.purge_due(deadline, 100)
    r = await row(sf, m.id)
    assert r["recipient_fp_digest"] is None and r["status"] == "unknown"
    assert r["payload_fp_digest"] is not None  # binding still kept
    assert await repo.claim_due("w1", deadline, LEASE, 10) == []  # never resent


async def test_receipt_after_match_deadline_is_unmatched_late(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    m = await sent_message(sf, keys, repo)
    late = T0 + DEFAULT_POLICY.receipt_window + timedelta(seconds=1)
    assert (
        await repo.record_receipt(receipt("Delivered", late), late)
        is ReceiptDisposition.UNMATCHED_LATE
    )
    assert (await row(sf, m.id))["status"] == "sent"


# -------------------------------------------------------------------- cleanup


async def test_a14_a20_purge_due_backstop_after_crash_and_outage(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    ids = []
    for i in range(5):
        m = await submit(repo, ctx, purpose="broadcast", expires_at=None, recipient=f"090000020{i}")
        ids.append(m.id)
    # Simulate crashes after a status commit but before the inline purge (content left).
    async with sf() as s, s.begin():
        await s.execute(
            update(messages)
            .where(messages.c.id.in_(ids))
            .values(
                status="sending",
                lease_token=__import__("uuid").uuid4(),
                attempts=1,
                last_attempt_at=T0,
            )
        )
        await s.execute(
            update(messages)
            .where(messages.c.id.in_(ids))
            .values(status="sent", sent_at=T0, provider_message_id="x")
        )
    assert all([await has_content(sf, i) for i in ids])
    # Outage: nothing ran for an hour. On recovery the backlog is cleared, in batches.
    later = T0 + timedelta(hours=1)
    assert await repo.purge_due(later, 2) >= 2
    await repo.purge_due(later, 2)
    await repo.purge_due(later, 2)
    assert not any([await has_content(sf, i) for i in ids])
    stamps = [(await row(sf, i))["content_purged_at"] for i in ids]
    assert stamps == [later] * len(ids)
    assert await repo.purge_due(later, 2) == 0  # idempotent


async def test_receipt_evidence_retention_and_content_key_retirement(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    await sent_message(sf, keys, repo)
    await repo.record_receipt(receipt("Delivered", T0), T0)
    await repo.purge_due(T0 + DEFAULT_POLICY.receipt_evidence_retention - timedelta(seconds=1), 10)
    async with sf() as s:
        assert len((await s.execute(select(receipts))).all()) == 1
    await repo.purge_due(T0 + DEFAULT_POLICY.receipt_evidence_retention, 10)
    async with sf() as s:
        assert (await s.execute(select(receipts))).all() == []
    # Content key c1 is current: refused. A pending message keeps an old key alive.
    with pytest.raises(KeyRetirementRefused):
        await repo.retire_key_version("content", "c1", T0)
    ctx = await caller(sf, keys, "svc2")
    await submit(repo, ctx, purpose="broadcast", expires_at=None)
    rotated = make_repo(
        sf,
        Secrets(
            __import__("oan_messaging.storage.crypto", fromlist=["ContentCipher"]).ContentCipher(
                {**keys.content_keys, "c2": b"\x09" * 32}, "c2"
            ),
            keys.fingerprinter,
            keys.pepper,
        ),
    )
    assert await rotated.content_key_versions_in_use() == frozenset({"c1"})
    with pytest.raises(KeyRetirementRefused):
        await rotated.retire_key_version("content", "c1", T0)


async def test_old_fingerprint_key_still_matches_receipts_after_rotation(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    m = await sent_message(sf, keys, repo)  # recipient fingerprint under f1
    rotated = make_repo(
        sf,
        Secrets(
            keys.cipher,
            HmacFingerprinter({**keys.fingerprint_keys, "f2": b"\x05" * 32}, "f2"),
            keys.pepper,
        ),
    )
    assert await rotated.record_receipt(receipt("Delivered", T0), T0) is ReceiptDisposition.APPLIED
    assert (await row(sf, m.id))["status"] == "delivered"
    async with sf() as s:
        (r,) = (await s.execute(select(receipts))).mappings().all()
    assert r["recipient_fp_version"] == "f1"
    assert await rotated.fingerprint_key_versions_in_use(T0) >= {"f1"}


async def test_status_trigger_allows_same_status_updates(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    async with sf() as s, s.begin():
        await s.execute(
            text("UPDATE messages SET status = 'accepted', updated_at = now() WHERE id = :i"),
            {"i": m.id},
        )
    assert (await row(sf, m.id))["status"] == "accepted"


async def test_stale_claim_cannot_finish_another_workers_attempt(
    sf: async_sessionmaker[AsyncSession], keys: Secrets, repo: PostgresMessageRepository
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (stale,) = await repo.claim_due("w1", T0, LEASE, 1)  # w1 stalls before begin_attempt
    (fresh,) = await repo.claim_due("w2", T0 + LEASE, LEASE, 1)  # lease taken over
    await repo.begin_attempt(fresh, T0 + LEASE)
    # w1 wakes up and reports a result for an attempt it does not own.
    with pytest.raises(LeaseLost):
        await repo.finish_attempt(
            stale, res(ProviderOutcome.SENT, T0 + LEASE, pid="w1-bogus"), None
        )
    r = await row(sf, m.id)
    assert r["status"] == "sending" and r["lease_token"] == fresh.claim_token
    with pytest.raises(LeaseLost):
        await repo.mark_expired(stale, T0 + LEASE)
    done = await repo.finish_attempt(fresh, res(ProviderOutcome.SENT, T0 + LEASE, pid="w2"), None)
    assert done.provider_message_id == "w2" and done.attempts == 1


async def test_a15_telegram_sent_purges_content_and_stays_sent(
    sf: async_sessionmaker[AsyncSession], keys: Secrets, repo: PostgresMessageRepository
) -> None:
    ctx = await caller(sf, keys, "grv", bot="grievance")
    m, _ = await repo.create_or_get(
        ctx, new_key(), TelegramSubmission.model_validate(tg_body()), T0
    )
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    tg = ProviderResult(
        outcome=ProviderOutcome.SENT,
        provider=Provider.TELEGRAM,
        provider_message_id="4711",
        provider_status="tg_200",
        started_at=T0,
        finished_at=T0,
    )
    done = await repo.finish_attempt(claim, tg, None)
    assert done.status is MessageStatus.SENT and not await has_content(sf, m.id)
    # Never final, never delivered, content gone; cleanup leaves it sent.
    await repo.purge_due(T0 + DEFAULT_POLICY.receipt_window, 100)
    r = await row(sf, m.id)
    assert r["status"] == "sent" and r["delivered_at"] is None and r["recipient_fp_digest"] is None
