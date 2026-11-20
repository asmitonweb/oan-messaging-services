"""MSG-07b regressions; synthetic providers and real disposable PostgreSQL."""

from __future__ import annotations

import io
import logging
import uuid
from datetime import timedelta

import pytest
from itsupport import Harness, new_key
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from oan_messaging.contracts import Channel, Provider, ProviderOutcome, ProviderResult
from oan_messaging.redaction import install_redaction
from oan_messaging.storage.schema import message_content
from oan_messaging.worker import WorkerConfig

pytestmark = pytest.mark.postgres


def test_default_batch_does_not_preclaim_twenty_messages() -> None:
    assert WorkerConfig().batch_size == 1


def test_late_handler_structured_extra_and_exception_are_redacted() -> None:
    secret = "synthetic-secret-for-msg07b"  # noqa: S105 (synthetic canary)
    install_redaction(secret)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s %(payload)s"))
    logger = logging.getLogger("httpcore.new_child_for_msg07b")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        try:
            raise ValueError(secret)
        except ValueError:
            logger.exception(
                "late %s", secret, extra={"payload": {"value": secret, "otp": "123456"}}
            )
        assert secret not in stream.getvalue()
        assert "123456" not in stream.getvalue()
        assert "<redacted>" in stream.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)


async def test_poison_row_does_not_block_healthy_work(h: Harness) -> None:
    token = await h.caller("poison", bot=None)
    bad = (await h.post_sms(token, new_key())).json()["id"]
    good = (await h.post_sms(token, new_key())).json()["id"]
    async with h.repo._sf() as session, session.begin():
        await session.execute(
            update(message_content)
            .where(message_content.c.message_id == uuid.UUID(bad))
            .values(key_version="missing")
        )
    await h.worker(batch_size=1).run_once()
    await h.worker(batch_size=1).run_once()
    assert (await h.status(token, good))["status"] == "sent"
    assert (await h.status(token, bad))["status"] == "accepted"
    async with h.repo._sf() as session:
        assert (
            await session.execute(
                select(message_content.c.message_id).where(
                    message_content.c.message_id == uuid.UUID(bad)
                )
            )
        ).scalar_one()


async def test_late_success_is_fenced_and_finish_is_idempotent(h: Harness) -> None:
    token = await h.caller("late", bot=None)
    mid = (await h.post_sms(token, new_key())).json()["id"]
    claim = (
        await h.repo.claim_due("late", h.clock(), timedelta(seconds=10), 1, channels=(Channel.SMS,))
    )[0]
    await h.repo.begin_attempt(claim, h.clock())
    h.clock.advance(seconds=11)
    await h.repo.recover_stale(h.clock())
    assert (await h.status(token, mid))["status"] == "unknown"
    result = ProviderResult(
        outcome=ProviderOutcome.SENT,
        provider=Provider.SMS_8028,
        provider_status="http_200",
        provider_message_id="late-proof-1",
        started_at=h.clock(),
        finished_at=h.clock(),
    )
    first = await h.repo.finish_attempt(claim, result, None)
    second = await h.repo.finish_attempt(claim, result, None)
    assert first.status == second.status == "sent"
    assert second.provider_message_id == "late-proof-1"


async def test_lost_finish_ack_does_not_send_twice(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await h.caller("finish", bot=None)
    mid = (await h.post_sms(token, new_key())).json()["id"]
    real = h.repo.finish_attempt
    calls = 0

    async def finish(*args, **kwargs):
        nonlocal calls
        calls += 1
        value = await real(*args, **kwargs)
        if calls == 1:
            raise OperationalError("synthetic", {}, Exception("lost commit ACK"))
        return value

    monkeypatch.setattr(h.repo, "finish_attempt", finish)
    await h.worker().run_once()
    assert calls == 2 and len(h.sms.calls) == 1
    assert (await h.status(token, mid))["status"] == "sent"


async def test_pilot_broadcast_denied_at_api_and_worker(h: Harness) -> None:
    token = await h.caller("broadcast-pilot", bot=None)
    h.app.state.deps.broadcast_enabled = False
    response = await h.post_sms(token, new_key(), purpose="broadcast", expires_at=None)
    assert response.status_code == 403
    h.app.state.deps.broadcast_enabled = True  # seed pre-existing future traffic
    mid = (await h.post_sms(token, new_key(), purpose="broadcast", expires_at=None)).json()["id"]
    assert (await h.worker(broadcast_enabled=False).run_once()).claimed == 0
    assert (await h.status(token, mid))["status"] == "accepted"
    assert not h.sms.calls


async def test_provider_auth_fault_suspends_backlog_until_operator_resume(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oan_messaging.contracts import FailureCode
    from oan_messaging.storage.schema import dispatch_controls

    token = await h.caller("scope", bot=None)
    for _ in range(3):
        await h.post_sms(token, new_key())
    worker = h.worker(batch_size=1)
    adapter = worker._adapters[Channel.SMS]

    async def refused(request):
        return ProviderResult(
            outcome=ProviderOutcome.NOT_SENT,
            provider=Provider.SMS_8028,
            failure_code=FailureCode.PROVIDER_AUTH,
            provider_status="http_401",
            started_at=h.clock(),
            finished_at=h.clock(),
        )

    monkeypatch.setattr(adapter, "send", refused)
    assert (await worker.run_once()).outcomes == {"failed": 1}
    assert (await h.worker(batch_size=1).run_once()).claimed == 0
    async with h.repo._sf() as session, session.begin():
        await session.execute(update(dispatch_controls).values(suspended=False))
    assert (await h.worker(batch_size=1).run_once()).outcomes == {"sent": 1}


async def test_local_lateness_does_not_exhaust_provider_retry_budget(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = await h.caller("late-local", bot=None)
    mid = (await h.post_sms(token, new_key(), purpose="transactional", expires_at=None)).json()[
        "id"
    ]
    worker = h.worker(batch_size=1)
    real = h.repo.begin_attempt

    async def slow(claim, now, **kw):
        result = await real(claim, now, **kw)
        h.clock.advance(seconds=1)
        return result

    monkeypatch.setattr(h.repo, "begin_attempt", slow)
    for _ in range(6):
        await worker.run_once()
        h.clock.advance(hours=1)
    assert not h.sms.calls
    assert (await h.status(token, mid))["status"] == "accepted"
    monkeypatch.setattr(h.repo, "begin_attempt", real)
    await worker.run_once()
    assert len(h.sms.calls) == 1
    assert (await h.status(token, mid))["status"] == "sent"


def test_uvicorn_access_log_lines_survive_redaction() -> None:
    """Found in the integration trial: redaction emptied record.args, so uvicorn's
    AccessFormatter raised on every request and the access log was lost."""
    from uvicorn.logging import AccessFormatter

    secret = "synthetic-secret-in-path"  # noqa: S105 (synthetic canary)
    install_redaction(secret)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    logger = logging.getLogger("uvicorn.access")
    logger.addHandler(handler)
    old_level, logger.level = logger.level, logging.INFO
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET",
                    f"/readyz?k={secret}", "1.1", 200)
        out = stream.getvalue()
        assert "GET /readyz" in out and "200" in out
        assert secret not in out and "<redacted>" in out
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
