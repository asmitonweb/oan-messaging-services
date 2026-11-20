"""Regressions for docs/reviews/MSG-02-review.md (real PostgreSQL, deterministic interleavings).

1. Callback / send-result commit race (P1).
2. Dispatch authorization at claim and at begin_attempt; Telegram bot binding (P1).
3. Streaming body limit without Content-Length (P2) — see also test_api_submission.py.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, MutableMapping
from datetime import timedelta
from typing import Any

import pytest
from conftest import T0, Secrets, headers, make_caller, make_repo, new_key, sms_body, tg_body
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_repository import LEASE, caller, has_content, receipt, res, row, submit

from oan_messaging.api import ApiDependencies, create_app
from oan_messaging.contracts import (
    Channel,
    MessageStatus,
    Provider,
    ProviderOutcome,
    ProviderResult,
    Purpose,
    ReceiptDisposition,
    TelegramSendRequest,
    TelegramSubmission,
)
from oan_messaging.storage.credentials import authenticate, upsert_caller
from oan_messaging.storage.errors import DispatchNotAuthorized
from oan_messaging.storage.repository import RECEIPT_LOCK_CLASS, PostgresMessageRepository
from oan_messaging.storage.schema import receipts

ALL_CH = frozenset(Channel)
ALL_PU = frozenset(Purpose)


async def wait_until(
    engine: AsyncEngine, sql: str, params: dict[str, Any] | None = None, timeout: float = 5.0
) -> None:
    """Poll pg_locks from an independent connection until ``sql`` returns > 0."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with engine.connect() as conn:
            if (await conn.execute(text(sql), params or {})).scalar_one() > 0:
                return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("expected lock wait did not happen")
        await asyncio.sleep(0.01)


WAITING_ON_RECEIPT_LOCK = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
    "AND classid = :c AND objsubid = 2"
)


# ------------------------------------------------------------- P1 callback race


async def test_receipt_during_uncommitted_send_result_is_not_stranded(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer interleaving, as two sessions: finish_attempt has scanned orphans but not
    committed when the callback arrives. The callback must wait for the send result and
    then apply, instead of committing an orphan that nothing will ever re-match."""
    ctx = await caller(sf, keys)
    msg = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    callback_repo = make_repo(sf, keys)  # independent sessions, as the ingress would have
    t1 = T0 + timedelta(seconds=1)
    holder: dict[str, asyncio.Task[ReceiptDisposition]] = {}
    original = repo._rematch_orphans

    async def interleaved(*args: Any, **kwargs: Any) -> None:
        await original(*args, **kwargs)  # orphan scan done; send transaction still open
        holder["cb"] = asyncio.create_task(
            callback_repo.record_receipt(receipt("Delivered", t1), t1)
        )
        # Deterministic: proceed only once the callback session is blocked on our lock.
        await wait_until(engine, WAITING_ON_RECEIPT_LOCK, {"c": RECEIPT_LOCK_CLASS})
        assert not holder["cb"].done()

    monkeypatch.setattr(repo, "_rematch_orphans", interleaved)
    done = await repo.finish_attempt(
        claim, res(ProviderOutcome.SENT, T0 + timedelta(seconds=2)), None
    )
    assert done.status is MessageStatus.SENT  # committed first
    disposition = await asyncio.wait_for(holder["cb"], 10)
    assert disposition is ReceiptDisposition.APPLIED
    stored = await repo.get_for_caller(ctx.caller_id, msg.id)
    assert stored is not None and stored.status is MessageStatus.DELIVERED
    async with sf() as s:
        (r,) = (await s.execute(select(receipts))).mappings().all()
    assert r["disposition"] == "applied" and r["message_id"] == msg.id


async def test_callback_before_send_response_still_rematched(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    """The other commit order: callback commits an orphan first, send result re-matches."""
    ctx = await caller(sf, keys)
    msg = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w1", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    assert await repo.record_receipt(receipt("Delivered", T0), T0) is ReceiptDisposition.ORPHANED
    done = await repo.finish_attempt(claim, res(ProviderOutcome.SENT, T0), None)
    assert done.status is MessageStatus.DELIVERED and not await has_content(sf, msg.id)


async def test_concurrent_callbacks_and_send_results_never_strand(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    """Many messages, callback and send result racing freely in both orders."""
    ctx = await caller(sf, keys)
    n = 20
    for i in range(n):
        await submit(repo, ctx, purpose="broadcast", expires_at=None, recipient=f"09000003{i:02d}")
    claims = await repo.claim_due("w1", T0, LEASE, n)
    for c in claims:
        await repo.begin_attempt(c, T0)
    other = make_repo(sf, keys)
    jobs: list[Awaitable[Any]] = []
    for i, c in enumerate(claims):
        pid = f"9{i:05d}"
        phone = c.send_request.recipient  # type: ignore[union-attr]
        jobs.append(repo.finish_attempt(c, res(ProviderOutcome.SENT, T0, pid=pid), None))
        jobs.append(other.record_receipt(receipt("Delivered", T0, pid=pid, phone=phone), T0))
    await asyncio.gather(*jobs)
    async with sf() as s:
        statuses = [r[0] for r in await s.execute(text("SELECT status FROM messages"))]
        disps = [r[0] for r in await s.execute(text("SELECT disposition FROM receipts"))]
    assert statuses == ["delivered"] * n
    assert disps == ["applied"] * n


# ------------------------------------------------------- P1 dispatch authorization


async def set_caller(
    sf: async_sessionmaker[AsyncSession],
    cid: str = "svc",
    *,
    channels: frozenset[Channel] = ALL_CH,
    purposes: frozenset[Purpose] = ALL_PU,
    bot: str | None = "grievance",
    enabled: bool = True,
) -> None:
    await upsert_caller(
        sf, cid, channels=channels, purposes=purposes, telegram_bot=bot, enabled=enabled
    )


@pytest.mark.parametrize(
    "revoke",
    [
        {"enabled": False},
        {"channels": frozenset({Channel.TELEGRAM})},  # SMS channel removed
        {"purposes": frozenset({Purpose.OTP})},  # broadcast purpose removed
        {"channels": frozenset(), "purposes": frozenset(), "enabled": False},
    ],
    ids=["disabled", "channel-removed", "purpose-removed", "everything-removed"],
)
async def test_sms_dispatch_stops_at_claim_and_resumes_when_restored(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    revoke: dict[str, Any],
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    await set_caller(sf, **revoke)
    assert await repo.claim_due("w", T0, LEASE, 10) == []
    r = await row(sf, m.id)
    assert r["status"] == "accepted" and r["payload_fp_digest"] is not None
    assert await has_content(sf, m.id)  # held, not purged
    await set_caller(sf)  # restored
    (claim,) = await repo.claim_due("w", T0, LEASE, 10)
    assert await repo.begin_attempt(claim, T0) == 1


async def test_reviewer_probe_disabled_caller_cannot_dispatch(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    await submit(repo, ctx)
    await upsert_caller(
        sf, "svc", channels=ctx.allowed_channels, purposes=ctx.allowed_purposes, enabled=False
    )
    assert await repo.claim_due("w", T0, LEASE, 1) == []


@pytest.mark.parametrize(
    "revoke",
    [
        {"enabled": False},
        {"channels": frozenset(), "purposes": frozenset(), "enabled": False},
        {"purposes": frozenset({Purpose.BROADCAST})},
        {"channels": frozenset({Channel.TELEGRAM})},
    ],
    ids=["disabled", "reviewer-probe-all-removed", "purpose-removed", "channel-removed"],
)
async def test_revocation_between_claim_and_begin_holds_the_message(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    revoke: dict[str, Any],
) -> None:
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx)  # OTP, expires T0+5m
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    await set_caller(sf, **revoke)
    with pytest.raises(DispatchNotAuthorized):
        await repo.begin_attempt(claim, T0)
    r = await row(sf, m.id)
    assert (r["status"], r["attempts"], r["lease_token"]) == ("accepted", 0, None)
    assert r["payload_fp_digest"] is not None and await has_content(sf, m.id)
    assert await repo.claim_due("w", T0, LEASE, 1) == []  # held
    await set_caller(sf)
    (again,) = await repo.claim_due("w", T0 + timedelta(seconds=1), LEASE, 1)
    assert await repo.begin_attempt(again, T0 + timedelta(seconds=1)) == 1


async def test_begin_attempt_is_the_authorization_boundary(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    await repo.begin_attempt(claim, T0)
    await set_caller(sf, enabled=False)  # after the provider operation began
    done = await repo.finish_attempt(claim, res(ProviderOutcome.SENT, T0), None)
    assert done.status is MessageStatus.SENT  # cannot be recalled


async def test_concurrent_revocation_is_ordered_against_begin_attempt(
    engine: AsyncEngine,
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    """A revocation in flight (uncommitted) when begin_attempt runs: begin waits on the
    caller row and then sees the committed revocation — no attempt slips through."""
    ctx = await caller(sf, keys)
    m = await submit(repo, ctx, purpose="broadcast", expires_at=None)
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    async with engine.connect() as admin:
        tx = await admin.begin()
        await admin.execute(text("UPDATE callers SET enabled = false WHERE caller_id = 'svc'"))
        task = asyncio.create_task(repo.begin_attempt(claim, T0))
        await wait_until(
            engine,
            "SELECT count(*) FROM pg_stat_activity WHERE "
            "wait_event_type = 'Lock' AND datname = current_database()",
        )
        assert not task.done()
        await tx.commit()
    with pytest.raises(DispatchNotAuthorized):
        await asyncio.wait_for(task, 10)
    assert (await row(sf, m.id))["status"] == "accepted"


async def tg_message(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
    *,
    bot: str = "bot-a",
    **body: Any,
) -> uuid.UUID:
    token = await make_caller(sf, keys, "svc", bot=bot)
    ctx = await authenticate(sf, keys.pepper, token)
    assert ctx is not None
    m, _ = await repo.create_or_get(
        ctx, new_key(), TelegramSubmission.model_validate(tg_body(**body)), T0
    )
    return m.id


async def test_telegram_bot_is_bound_at_acceptance_never_retargeted(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    mid = await tg_message(sf, keys, repo, bot="bot-a")
    assert (await row(sf, mid))["authorized_bot"] == "bot-a"
    for changed in ("bot-b", None):
        await set_caller(sf, bot=changed)
        assert await repo.claim_due("w", T0, LEASE, 10) == []  # never sent via bot-b
        r = await row(sf, mid)
        assert r["status"] == "accepted" and r["authorized_bot"] == "bot-a"
    await set_caller(sf, bot="bot-a")  # original mapping restored
    (claim,) = await repo.claim_due("w", T0, LEASE, 10)
    assert isinstance(claim.send_request, TelegramSendRequest)
    assert claim.send_request.bot == "bot-a"
    assert await repo.begin_attempt(claim, T0) == 1


async def test_telegram_bot_change_between_claim_and_begin(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    mid = await tg_message(sf, keys, repo, bot="bot-a")
    (claim,) = await repo.claim_due("w", T0, LEASE, 1)
    await set_caller(sf, bot="bot-b")
    with pytest.raises(DispatchNotAuthorized):
        await repo.begin_attempt(claim, T0)
    assert (await row(sf, mid))["status"] == "accepted"
    await set_caller(sf, bot="bot-a")
    (again,) = await repo.claim_due("w", T0, LEASE, 1)
    await repo.begin_attempt(again, T0)
    tg = ProviderResult(
        outcome=ProviderOutcome.SENT,
        provider=Provider.TELEGRAM,
        provider_message_id="4711",
        provider_status="tg_200",
        started_at=T0,
        finished_at=T0,
    )
    assert (await repo.finish_attempt(again, tg, None)).status is MessageStatus.SENT


async def test_held_messages_keep_normal_expiry_and_non_expiring_stay_held(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    ctx = await caller(sf, keys)
    otp = await submit(repo, ctx)  # expires T0+5m
    forever = await submit(repo, ctx, purpose="broadcast", expires_at=None, recipient="0900000002")
    await set_caller(sf, enabled=False)
    assert await repo.claim_due("w", T0 + timedelta(minutes=4), LEASE, 10) == []
    assert (await row(sf, otp.id))["status"] == "accepted"  # not mislabelled before expiry
    assert await repo.claim_due("w", T0 + timedelta(minutes=5), LEASE, 10) == []
    r = await row(sf, otp.id)
    assert r["status"] == "expired" and not await has_content(sf, otp.id)
    later = T0 + timedelta(days=60)
    await repo.purge_due(later, 100)
    assert await repo.claim_due("w", later, LEASE, 10) == []
    f = await row(sf, forever.id)
    # Documented limitation: a non-expiring held message stays accepted (binding kept).
    assert f["status"] == "accepted" and f["payload_fp_digest"] is not None


async def test_api_records_authorized_bot_and_replay_survives_bot_change(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    import httpx

    token = await make_caller(sf, keys, "svc", bot="bot-a")

    async def auth(t: str) -> Any:
        return await authenticate(sf, keys.pepper, t)

    app = create_app(ApiDependencies(repository=repo, authenticate=auth, clock=lambda: T0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        key = new_key()
        first = await c.post(
            "/api/v1/messages/telegram", json=tg_body(), headers=headers(token, key)
        )
        assert first.status_code == 202
        await set_caller(sf, bot="bot-b")
        replay = await c.post(
            "/api/v1/messages/telegram", json=tg_body(), headers=headers(token, key)
        )
        assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
        new = await c.post(
            "/api/v1/messages/telegram", json=tg_body(), headers=headers(token, new_key())
        )
        assert new.status_code == 202
    assert (await row(sf, uuid.UUID(first.json()["id"])))["authorized_bot"] == "bot-a"
    assert (await row(sf, uuid.UUID(new.json()["id"])))["authorized_bot"] == "bot-b"


# ---------------------------------------------------------- P2 streaming body limit


def asgi_scope(token: str | None, key: str) -> dict[str, Any]:
    hdrs = [(b"content-type", b"application/json"), (b"idempotency-key", key.encode())]
    if token:
        hdrs.append((b"authorization", f"Bearer {token}".encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/messages/sms",
        "raw_path": b"/api/v1/messages/sms",
        "root_path": "",
        "query_string": b"",
        "headers": hdrs,
        "server": ("testserver", 80),
        "client": ("client", 1),
    }


async def run_streaming(
    app: Any, scope: dict[str, Any], chunk: bytes, total_chunks: int
) -> tuple[int, int, bytes]:
    pulled = 0

    async def receive() -> dict[str, Any]:
        nonlocal pulled
        pulled += 1
        if pulled > total_chunks:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": chunk, "more_body": pulled < total_chunks}

    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], pulled, body


async def test_streamed_oversized_body_without_content_length_stops_early(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    token = await make_caller(sf, keys, "svc")

    async def auth(t: str) -> Any:
        return await authenticate(sf, keys.pepper, t)

    app = create_app(
        ApiDependencies(
            repository=repo, authenticate=auth, clock=lambda: T0, max_body_bytes=16 * 1024
        )
    )
    chunk = b" " * 4096
    status, pulled, body = await run_streaming(app, asgi_scope(token, new_key()), chunk, 1000)
    assert status == 413 and b"payload_too_large" in body
    # 16 KiB limit, 4 KiB chunks: exceeded on the 5th chunk; the other 995 never read.
    assert pulled == 5, pulled
    # Authentication still precedes reading the body.
    status, pulled, _ = await run_streaming(
        app, asgi_scope("oanmsg_" + "B" * 43, new_key()), chunk, 1000
    )
    assert status == 401 and pulled == 0
    async with sf() as s:
        assert (await s.execute(text("SELECT count(*) FROM messages"))).scalar_one() == 0


async def test_streamed_body_within_limit_is_accepted(
    sf: async_sessionmaker[AsyncSession],
    keys: Secrets,
    repo: PostgresMessageRepository,
) -> None:
    import json

    token = await make_caller(sf, keys, "svc")

    async def auth(t: str) -> Any:
        return await authenticate(sf, keys.pepper, t)

    app = create_app(ApiDependencies(repository=repo, authenticate=auth, clock=lambda: T0))
    payload = json.dumps(sms_body()).encode()
    pieces = [payload[i : i + 7] for i in range(0, len(payload), 7)]
    it = iter(pieces)
    pulled = 0

    async def receive() -> dict[str, Any]:
        nonlocal pulled
        pulled += 1
        piece = next(it, None)
        if piece is None:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": piece, "more_body": pulled < len(pieces)}

    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(asgi_scope(token, new_key()), receive, send)
    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 202
