"""Leakage checks over the composed service.

* Logs at DEBUG for EVERY logger (root capture, so child loggers and third-party loggers
  are included), with exception tracebacks formatted, across API submission, real
  httpx/httpcore requests over loopback TLS (8028 mTLS and Telegram), receipts, error
  paths and a worker crash raised by the real database driver.
* Token redaction for child loggers created after the adapter (failing regression test).
* A database-wide plaintext scan after a mixed run and cleanup.

``test_defect_*`` tests are failing regression tests documented in docs/test-report.md.
"""

from __future__ import annotations

import logging
import secrets
import ssl
import traceback
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from itsupport import (
    OTP_TEXT,
    Certs,
    Harness,
    TlsFakeProvider,
    callback,
    dump_database_text,
    fake_number,
    hdr,
    hex_forms,
    needle_forms,
    new_key,
    start_ingress,
    tg_body,
)
from pydantic import SecretStr

from oan_messaging.adapters.sms8028 import Sms8028Adapter, Sms8028Settings, make_client
from oan_messaging.adapters.telegram import TelegramAdapter, TelegramBotRegistry
from oan_messaging.contracts import Channel
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.worker import RateLimit, SharedRateLimiter, Worker, WorkerConfig, run_cleanup
from oan_messaging.worker.rate_limit import clock_time_source

pytestmark = pytest.mark.postgres


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))  # includes formatted exc_info tracebacks

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def capture() -> Iterator[Capture]:
    root = logging.getLogger()
    handler = Capture()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(old)


def needles(h: Harness, tokens: list[str], numbers: list[str], chats: list[int]) -> list[str]:
    out = [OTP_TEXT, "482913", *h.keys.secret_needles(), *tokens]
    for n in numbers:
        out += needle_forms(n)
    out += [str(c) for c in chats]
    return out


def assert_clean(text: str, items: list[str]) -> None:
    leaked = [i for i in items if i and i in text]
    assert not leaked, f"{len(leaked)} sensitive value(s) found"


def real_adapters(
    h: Harness, certs: Certs, gw: TlsFakeProvider, tgw: TlsFakeProvider
) -> dict[Channel, Any]:
    sms_settings = Sms8028Settings(
        client_secret=SecretStr(h.keys.sms_secret),
        client_cert=certs["gwclient_cert"],
        client_key=certs["gwclient_key"],
        ca_cert=certs["ca_cert"],
        base_url=f"https://localhost:{gw.port}/sms-api/v1",
        connect_timeout=2.0,
        read_timeout=5.0,
    )
    tg_client = httpx.AsyncClient(
        verify=ssl.create_default_context(cafile=str(certs["ca_cert"])),
        follow_redirects=False,
        trust_env=False,
        timeout=5.0,
    )
    registry = TelegramBotRegistry({k: SecretStr(v) for k, v in h.keys.bot_tokens.items()})
    return {
        Channel.SMS: Sms8028Adapter(sms_settings, make_client(sms_settings), clock=h.clock),
        Channel.TELEGRAM: TelegramAdapter(
            registry, tg_client, api_base_url=f"https://localhost:{tgw.port}", clock=h.clock
        ),
    }


async def test_debug_logs_and_tracebacks_across_the_real_stack_leak_nothing(
    h: Harness,
    certs: Certs,
    capture: Capture,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = TlsFakeProvider(certs, require_client_cert=True, mode="ok")
    tgw = TlsFakeProvider(certs, require_client_cert=False, mode="telegram")
    limiter = SharedRateLimiter(
        h.engine,
        {Channel.SMS: RateLimit(1000), Channel.TELEGRAM: RateLimit(1000)},
        time_source=clock_time_source(h.clock),
        monotonic=h.clock.monotonic,
        sleep=h.clock.sleep,
    )
    adapters = real_adapters(h, certs, gw, tgw)
    try:
        worker = Worker(
            h.repo,
            adapters,
            limiter,
            WorkerConfig(worker_id="leak", batch_size=20),
            clock=h.clock,
            sleep=h.clock.sleep,
        )
        esignet = await h.caller("esignet", bot=None)
        grievance = await h.caller("grievance", bot="grievance")
        mid = (await h.post_sms(esignet, new_key())).json()["id"]
        await h.post_tg(grievance, new_key(), chat_id=700001)

        # Hostile/malformed submissions whose values must never be echoed or logged.
        bad: list[dict[str, Any]] = [
            dict(recipient="0900000101x", text=OTP_TEXT),
            dict(text=OTP_TEXT * 20),
            {h.keys.bot_tokens["da"]: 1},
        ]
        for extra in bad:
            r = await h.post_sms(esignet, new_key(), **extra)
            assert r.status_code == 422
            assert_clean(r.text, needles(h, [esignet], [fake_number(101)], []))
        r = await h.api.post(
            "/api/v1/messages/sms",
            json={"recipient": "0900000101"},
            headers=hdr(esignet, "0900000101-482913"),
        )
        assert r.status_code == 422 and "0900000101" not in r.text
        r = await h.api.post(
            "/api/v1/messages/telegram",
            json=tg_body(token=h.keys.bot_tokens["da"]),
            headers=hdr(grievance, new_key()),
        )
        assert r.status_code == 422

        await worker.run_once()  # real httpx/httpcore over TLS, real adapter logging
        assert len(gw.requests) == 1 and len(tgw.requests) == 1
        assert (await h.status(esignet, mid))["status"] == "sent"

        ingress = await start_ingress(h.repo, certs, h.clock)
        try:
            await ingress.post(callback("991", fake_number(101), "Delivered", h.clock()))
            await ingress.post(callback("991", fake_number(101), "Odd status", h.clock()))
            await ingress.post(b'{"sms_id": "991", "phone_number": "0900000101"}')
        finally:
            await ingress.server.close()

        # API storage failure raised by the real driver (wrong password) -> 503.
        bad_engine = make_engine(database_url.replace("@", "x@", 1))
        monkeypatch.setattr(h.repo, "_sf", make_session_factory(bad_engine))
        r = await h.post_sms(esignet, new_key(), recipient=fake_number(102))
        assert r.status_code == 503
        monkeypatch.undo()
        await bad_engine.dispose()

        # Worker crash after a successful provider call: finish_attempt fails inside the
        # real driver; format the propagated exception as a supervisor would.
        mid2 = (await h.post_sms(esignet, new_key(), recipient=fake_number(103))).json()["id"]
        real_finish = h.repo.finish_attempt
        dead = make_engine(database_url.rsplit("/", 1)[0] + "/oan_msg_does_not_exist")

        async def finish_on_dead_db(*a: Any, **k: Any) -> Any:
            h.repo._sf = make_session_factory(dead)  # noqa: SLF001
            return await real_finish(*a, **k)

        live_sf = h.repo._sf  # noqa: SLF001
        monkeypatch.setattr(h.repo, "finish_attempt", finish_on_dead_db)
        with pytest.raises(Exception) as crash:  # noqa: PT011
            await worker.run_once()
        logging.getLogger("it.supervisor").error("worker cycle crashed", exc_info=crash.value)
        formatted = "".join(traceback.format_exception(crash.value))
        monkeypatch.undo()
        h.repo._sf = live_sf  # noqa: SLF001
        await dead.dispose()
        assert (await h.status(esignet, mid2))["status"] == "sending"  # recovery -> unknown
    finally:
        await limiter.aclose()
        for adapter in adapters.values():
            await adapter._client.aclose()  # noqa: SLF001
        gw.close()
        tgw.close()

    sensitive = needles(
        h,
        [esignet, grievance, database_url.split(":")[2].split("@")[0]],
        [fake_number(101), fake_number(102), fake_number(103)],
        [700001],
    )
    assert any("httpx" in line or "httpcore" in line for line in capture.lines)
    assert_clean(capture.text, sensitive)
    assert_clean(formatted, sensitive)


def test_defect_token_redaction_misses_child_loggers_created_after_the_adapter(
    keys_tokens: dict[str, str], capture: Capture
) -> None:
    """``install_token_redaction`` (telegram.py:116-129) attaches its filter to the
    ``httpx``/``httpcore`` loggers that exist when an adapter is built. Logger filters do
    not run for records propagated from child loggers, so a child created later (httpcore
    imports ``httpcore.http2``/``httpcore.socks`` lazily when h2/socksio are present)
    bypasses redaction. Its docstring promises that httpcore children are covered. The
    8028 adapter's secret filter (sms8028.py:271-277) never covers children at all.
    Today's httpcore trace lines carry no URL or body, so this is defense in depth."""
    token = keys_tokens["grievance"]
    TelegramAdapter(
        TelegramBotRegistry({"grievance": SecretStr(token)}),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        api_base_url="https://api.telegram.invalid",
    )
    name = "httpcore.http2"
    if name in logging.root.manager.loggerDict:
        name = f"httpcore.lazy_{secrets.token_hex(3)}"
    logging.getLogger(name).debug("request url=https://api.telegram.org/bot%s/sendMessage", token)
    leaked = token in capture.text  # bool only: never print the (generated) token
    assert not leaked, "bot token reached the root handler unredacted"


def test_finding_production_order_redacts_bot_tokens_on_existing_httpcore_children(
    keys_tokens: dict[str, str], capture: Capture
) -> None:
    """Finding C scope (passes). In production construction order (real
    ``telegram.make_client()`` first, then the adapter) every httpcore child that exists
    gets the token filter, so the gap needs a child logger created AFTER composition
    (``httpcore.http2``/``httpcore.socks`` only exist when the optional h2/socksio packages
    are installed; they are not installed here)."""
    from oan_messaging.adapters import telegram as tg

    token = keys_tokens["grievance"]
    client = tg.make_client()
    TelegramAdapter(
        TelegramBotRegistry({"grievance": SecretStr(token)}),
        client,
        api_base_url="https://api.telegram.invalid",
    )
    children = [n for n in logging.root.manager.loggerDict if n.startswith("httpcore.")]
    assert children  # httpcore.connection / http11 / proxy exist in production
    for name in children:
        logging.getLogger(name).debug("url=https://api.telegram.org/bot%s/sendMessage", token)
    assert token not in capture.text


def test_defect_8028_secret_filter_does_not_cover_existing_httpcore_children(
    capture: Capture,
) -> None:
    """Finding C, 8028 part (FAILS). The 8028 adapter attaches its client-secret filter only
    to ``httpx``, ``httpcore`` and its own logger (sms8028.py:271-277). Records from
    ``httpcore.connection``/``http11``/``proxy``, which exist in every production
    composition, are not filtered. Synthetic probe with a generated secret; httpcore does
    not log request bodies today (see the real-stack DEBUG test above), so no actual
    exposure was observed. Owner: MSG-08 runtime logging (handler-level redaction)."""
    import httpx as _httpx

    secret = "it8028-" + secrets.token_urlsafe(24)
    client = _httpx.AsyncClient()  # default transport: creates the httpcore children
    Sms8028Adapter(
        Sms8028Settings(
            client_secret=SecretStr(secret),
            client_cert=Path("/nonexistent/c"),
            client_key=Path("/nonexistent/k"),
        ),
        client,
    )
    for name in ("httpcore.connection", "httpcore.http11"):
        logging.getLogger(name).debug("body clientSecret=%s", secret)
    leaked = secret in capture.text  # bool only: never print the generated secret
    assert not leaked, "8028 client secret reached the root handler unredacted"


@pytest.fixture
def keys_tokens() -> dict[str, str]:
    from itsupport import new_bot_token

    return {"grievance": new_bot_token()}


async def test_database_holds_no_plaintext_after_a_mixed_run_and_cleanup(
    h: Harness, certs: Certs, database_url: str
) -> None:
    esignet = await h.caller("esignet", bot=None)
    grievance = await h.caller("grievance", bot="grievance")
    texts = {n: f"Your OAN code is {700000 + n}." for n in range(150, 156)}
    ids: dict[int, str] = {}
    for n, text in texts.items():
        ids[n] = (await h.post_sms(esignet, new_key(), recipient=fake_number(n), text=text)).json()[
            "id"
        ]
    tg_text = "Grievance G-9 closed by officer."
    await h.post_tg(grievance, new_key(), chat_id=812345678, text=tg_text)

    def script(body: dict[str, Any], n: int) -> httpx.Response:
        phone = body["phonenums"][0]
        if phone == fake_number(151):
            return httpx.Response(503, json={})
        if phone == fake_number(152):
            return httpx.Response(400, json={"message": "Invalid phone number"})
        return h.sms.ok(body)

    h.sms.script = script
    await h.worker().run_once()
    sms_id_150 = (
        await h.one("SELECT provider_message_id FROM messages WHERE id = :i", i=ids[150])
    )[0]
    ingress = await start_ingress(h.repo, certs, h.clock)
    try:
        await ingress.post(callback(sms_id_150, fake_number(150), "Delivered", h.clock()))
        await ingress.post(callback("424242", fake_number(153), "Sent", h.clock()))  # orphan
    finally:
        await ingress.server.close()
    late = (
        await h.post_sms(
            esignet,
            new_key(),
            recipient=fake_number(156),
            text="Your OAN code is 799999.",
            expires_at=(h.clock() + timedelta(minutes=1)).isoformat(),
        )
    ).json()["id"]
    h.clock.advance(minutes=2)
    await h.worker().run_once()
    await run_cleanup(h.repo, h.clock())
    # Still queued (not yet dispatched): its content must exist, encrypted.
    pending = "Pending advisory text."
    texts[157] = pending
    await h.post_sms(
        esignet,
        new_key(),
        recipient=fake_number(157),
        text=pending,
        purpose="broadcast",
        expires_at=None,
    )
    statuses = {r[0] for r in await h.rows("SELECT status FROM messages")}
    assert {"delivered", "sent", "unknown", "failed", "expired"} <= statuses
    assert (await h.status(esignet, late))["status"] == "expired"

    dump = dump_database_text(database_url)
    sensitive = [
        *texts.values(),
        tg_text,
        "Your OAN code is 799999.",
        "812345678",
        *h.keys.secret_needles(),
        esignet,
        grievance,
    ]
    for n in range(150, 158):
        sensitive += needle_forms(fake_number(n))
    for t in [*texts.values(), tg_text]:
        sensitive += hex_forms(t)
    assert_clean(dump, sensitive)
    # Only messages still awaiting a send keep (encrypted) content.
    content = await h.rows(
        "SELECT m.status FROM message_content c JOIN messages m ON m.id = c.message_id"
    )
    assert content and {r[0] for r in content} <= {"accepted"}


def test_capture_helper_sees_child_logger_records(capture: Capture) -> None:
    logging.getLogger("oan_messaging.it.child").warning("probe-%s", "ok")
    assert "probe-ok" in capture.text
    assert Path(__file__).name.startswith("test_sec_")
