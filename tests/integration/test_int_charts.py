"""GET /api/v1/charts: statistics for the OAN dashboards (the dashboard service contract of
the A2C and Grievance services), from the snapshot the cleanup component rebuilds."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from itsupport import Harness, fake_number, hdr, http_status, new_key
from sqlalchemy import text

from oan_messaging.stats.charts import CHARTS, percentile
from oan_messaging.stats.snapshot import LOCK_KEY, refresh


async def charts(h: Harness, token: str, path: str, **params: Any) -> httpx.Response:
    return await h.api.get(f"/api/v1/charts{path}", params=params, headers=hdr(token))


async def txn(h: Harness, token: str, n: int, **kw: Any) -> str:
    body = {"purpose": "transactional", "expires_at": None, "text": f"Update {n}", **kw}
    r = await h.post_sms(token, new_key(), recipient=fake_number(n), **body)
    assert r.status_code == 202, r.text
    return str(r.json()["id"])


async def seed(h: Harness) -> dict[str, str]:
    """a2c: one delivered, one rejected by 8028, one unknown (503); esignet: one OTP sent."""
    a2c = await h.caller("a2c", bot=None)
    esignet = await h.caller("esignet", bot=None)
    rejected, unknown = fake_number(302), fake_number(303)

    def gateway(body: dict[str, Any], n: int) -> httpx.Response:
        phone = body["phonenums"][0]
        if phone == rejected:
            return http_status(400, {"success": False, "message": "Invalid phone"})
        if phone == unknown:
            return http_status(503)
        return h.sms.ok(body)

    h.sms.script = gateway
    ids = {
        "delivered": await txn(h, a2c, 301),
        "rejected": await txn(h, a2c, 302),
        "unknown": await txn(h, a2c, 303),
    }
    otp = await h.post_sms(esignet, new_key(), recipient=fake_number(304))
    assert otp.status_code == 202
    h.clock.advance(seconds=3)
    await h.worker(batch_size=10).run_once()
    delivered_at = h.clock() + timedelta(seconds=40)
    async with h.engine.begin() as conn:
        await conn.execute(
            text("UPDATE messages SET status = 'delivered', delivered_at = :t WHERE id = :id"),
            {"t": delivered_at, "id": ids["delivered"]},
        )
    return {"a2c": a2c, "esignet": esignet}


async def reader(h: Harness) -> str:
    return await h.caller("reports-central", stats_reader=True)


async def test_only_a_statistics_reader_may_read_and_a_reader_cannot_send(h: Harness) -> None:
    sender = await h.caller("a2c", bot=None)
    token = await reader(h)
    assert (await h.api.get("/api/v1/charts/msgKpis")).status_code == 401
    r = await charts(h, sender, "/msgKpis")
    assert r.status_code == 403 and r.json()["errors"] == [{"code": "stats_not_permitted"}]
    assert (await charts(h, token, "/msgKpis")).status_code == 200
    sent = await h.post_sms(token, new_key(), purpose="transactional", expires_at=None)
    assert sent.status_code == 403


async def test_kpis_by_caller_by_purpose_and_failure_reasons(h: Harness) -> None:
    await seed(h)
    token = await reader(h)
    assert await refresh(h.sf, h.clock())

    kpis = (await charts(h, token, "/msgKpis")).json()
    assert kpis["success"] is True and kpis["meta"]["chart"] == "msgKpis"
    assert kpis["meta"]["as_of"] is not None
    [k] = kpis["data"]
    assert (k["accepted"], k["sent"], k["delivered"], k["failed"], k["unknown"], k["open"]) == (
        4, 1, 1, 1, 1, 0,
    )
    assert k["delivered_share"] == 0.25 and k["deliver_p50_s"] == 60  # 43 s -> the 30-60 s bucket

    by_caller = {r["caller"]: r for r in (await charts(h, token, "/msgByCaller")).json()["data"]}
    assert by_caller["a2c"]["accepted"] == 3 and by_caller["esignet"]["sent"] == 1
    by_purpose = [r["purpose"] for r in (await charts(h, token, "/msgByPurpose")).json()["data"]]
    assert by_purpose == ["otp", "transactional"]
    reasons = (await charts(h, token, "/msgFailureReasons")).json()["data"]
    assert [(r["failure_code"], r["total"], r["share"]) for r in reasons] == [
        ("invalid_recipient", 1, 0.5),  # 400 from 8028: failed
        ("provider_server_error", 1, 0.5),  # 503: unknown, never resent
    ]
    otp = (await charts(h, token, "/msgOtpLatency")).json()["data"]
    assert len(otp) == 1 and otp[0]["sent_or_delivered"] == 1 and otp[0]["send_p95_s"] is not None

    only_a2c = (await charts(h, token, "/msgKpis", caller="a2c")).json()["data"][0]
    assert only_a2c["accepted"] == 3
    trend = (await charts(h, token, "/msgTrend", granularity="month")).json()["data"]
    assert [t["period"] for t in trend] == ["2026-09-01"] and trend[0]["accepted"] == 4


async def test_batch_unknown_chart_and_invalid_filters(h: Harness) -> None:
    token = await reader(h)
    r = await charts(h, token, "", charts="msgKpis,msgByPurpose,msgKpis")
    body = r.json()
    assert r.status_code == 200 and list(body["data"]) == ["msgKpis", "msgByPurpose"]
    assert body["data"]["msgKpis"]["chartName"] == "msgKpis"
    assert (await charts(h, token, "/nope")).status_code == 404
    bad = await charts(h, token, "/msgTrend", granularity="year")
    assert bad.status_code == 422 and bad.json()["errors"] == [
        {"field": "granularity", "code": "enum"}
    ]
    later = await charts(h, token, "/msgKpis", **{"from": "2026-09-10", "to": "2026-09-01"})
    assert later.json()["errors"] == [{"field": "from", "code": "after_to"}]
    assert (await charts(h, token, "/msgKpis", purpose="spam")).status_code == 422
    assert (await charts(h, token, "")).status_code == 422
    # Dashboards pass their own filters (region, ...): ignored.
    assert (await charts(h, token, "/msgKpis", region="ET04")).status_code == 200
    # Before the first refresh: zero rows and no as_of.
    assert (await charts(h, token, "/msgKpis")).json()["meta"]["as_of"] is None


async def test_campaigns_group_a_broadcast_by_its_reference(h: Harness) -> None:
    da = await h.caller("da", bot=None)
    for n in (401, 402, 403):
        await txn(h, da, n, purpose="broadcast", source_reference=f"da_broadcast:BC-7:FR-{n}")
    await txn(h, da, 404, purpose="alert", source_reference="da_broadcast:BC-8:FR-404")  # one only
    await h.worker(batch_size=10).run_once()
    token = await reader(h)
    assert await refresh(h.sf, h.clock())
    rows = (await charts(h, token, "/msgCampaigns")).json()["data"]
    assert [(r["campaign"], r["purpose"], r["recipients"], r["sent"]) for r in rows] == [
        ("da_broadcast:BC-7", "broadcast", 3, 3)
    ]


async def test_queue_and_provider_health_are_live(h: Harness) -> None:
    a2c = await h.caller("a2c", bot=None)
    await txn(h, a2c, 501)
    h.clock.advance(seconds=90)
    await txn(h, a2c, 502)
    async with h.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dispatch_controls (channel, suspended, updated_at) "
                "VALUES ('sms', true, :t)"
            ),
            {"t": h.clock()},
        )
    token = await reader(h)
    q = (await charts(h, token, "/msgQueueNow")).json()
    assert q["data"] == [
        {
            "purpose": "transactional",
            "channel": "sms",
            "waiting": 2,
            "due_now": 2,
            "sending": 0,
            "oldest_wait_s": 90,
        }
    ]
    assert q["meta"]["as_of"] is not None  # live charts carry the request time
    rows = (await charts(h, token, "/msgProviderHealth")).json()["data"]
    health = {r["channel"]: r for r in rows}
    assert health["sms"]["suspended"] is True and health["telegram"]["suspended"] is False
    callers_ = (await charts(h, token, "/msgFilterCallers")).json()["data"]
    assert callers_ == [{"caller": "a2c"}]  # the reader itself is not a sending caller


async def test_refresh_keeps_final_days_and_never_runs_twice_at_once(h: Harness) -> None:
    a2c = await h.caller("a2c", bot=None)
    await txn(h, a2c, 601)
    assert await refresh(h.sf, h.clock())
    h.clock.advance(days=20)
    await txn(h, a2c, 602)
    assert await refresh(h.sf, h.clock())
    days = (await h.one("SELECT count(DISTINCT day) FROM stats_daily"))[0]
    assert days == 2  # the old day is final and kept; the new one is added
    async with h.engine.connect() as conn, conn.begin():
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        assert await refresh(h.sf, h.clock()) is False


def test_percentile_reads_the_bucket_upper_edge() -> None:
    hist = [0] * 14
    assert percentile(hist, 0.5) is None
    hist[0], hist[5] = 9, 1  # nine under 1 s, one between 30 and 60 s
    assert (percentile(hist, 0.5), percentile(hist, 0.95)) == (1, 60)
    assert set(CHARTS) >= {"msgKpis", "msgCampaigns", "msgQueueNow", "msgProviderHealth"}
