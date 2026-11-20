"""MSG-04 Telegram adapter tests. Every Bot API call is mocked (httpx.MockTransport);
real sockets are blocked for the whole module. Tokens below are fake."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from oan_messaging.adapters.telegram import (
    TOKEN_ENV_PREFIX,
    TelegramAdapter,
    TelegramBotRegistry,
    make_client,
    redact_tokens,
)
from oan_messaging.contracts import (
    CHANNEL_DELIVERY_RECEIPTS,
    OUTCOME_TO_STATUS,
    Channel,
    FailureCode,
    MessageAdapter,
    MessageStatus,
    Provider,
    ProviderOutcome,
    ProviderResult,
    SmsSendRequest,
    TelegramSendRequest,
)

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijk"  # noqa: S105 (fake test token)
OTHER_TOKEN = "987654321:BBOtherFakeTokenForTests_lmnopqrstu"  # noqa: S105 (fake test token)
NOW = datetime(2026, 9, 1, 5, 12, 13, tzinfo=UTC)
CHAT = 555000111

Handler = Callable[[httpx.Request], httpx.Response]


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("real network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def registry() -> TelegramBotRegistry:
    return TelegramBotRegistry(
        {"grievance": SecretStr(FAKE_TOKEN), "advisory": SecretStr(OTHER_TOKEN)}
    )


def adapter_for(handler: Handler) -> tuple[TelegramAdapter, Recorder]:
    rec = Recorder(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    return TelegramAdapter(registry(), client, clock=lambda: NOW), rec


def req(text: str = "Your grievance G-1042 was updated.", **kw: Any) -> TelegramSendRequest:
    data: dict[str, Any] = {"message_id": uuid.uuid4(), "attempt": 1, "bot": "grievance",
                            "chat_id": CHAT, "text": text}
    data.update(kw)
    return TelegramSendRequest(**data)


def ok_body(message_id: Any = 4711, chat_id: Any = CHAT) -> dict[str, Any]:
    return {"ok": True, "result": {"message_id": message_id, "date": 1788246733,
                                   "chat": {"id": chat_id, "type": "private"},
                                   "text": "echo of text"}}


def respond(status: int, body: Any = None, *, raw: bytes | None = None) -> Handler:
    def handler(_r: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=body)
    return handler


def tg_error(code: int, description: str, **params: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return body


def assert_no_leak(result: ProviderResult, *needles: str) -> None:
    dumped = result.model_dump_json()
    for needle in (FAKE_TOKEN, OTHER_TOKEN, "bot123456789:", "bot987654321:", *needles):
        assert needle not in dumped


# ----------------------------------------------------------------------- contract fit


def test_implements_frozen_adapter_protocol() -> None:
    adapter, _ = adapter_for(respond(200, ok_body()))
    assert isinstance(adapter, MessageAdapter)
    assert adapter.provider is Provider.TELEGRAM and adapter.channel is Channel.TELEGRAM


async def test_success_is_sent_not_delivered_and_preserves_message_id() -> None:
    adapter, rec = adapter_for(respond(200, ok_body(message_id=4711)))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.SENT
    assert result.provider_message_id == "4711"
    assert result.provider_status == "tg_200" and result.failure_code is None
    assert result.started_at == NOW and result.finished_at == NOW
    # Sent is the strongest Telegram outcome: never delivered.
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.SENT
    assert CHANNEL_DELIVERY_RECEIPTS[Channel.TELEGRAM] is False
    # Exactly one plain-text sendMessage call to the configured bot.
    assert len(rec.requests) == 1
    sent = rec.requests[0]
    assert sent.method == "POST"
    assert sent.url == httpx.URL(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage")
    assert json.loads(sent.content) == {"chat_id": CHAT, "text": req().text}
    assert_no_leak(result, req().text, str(CHAT))


async def test_uses_only_the_requested_configured_bot() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    await adapter.send(req(bot="advisory"))
    assert rec.requests[0].url.path == f"/bot{OTHER_TOKEN}/sendMessage"


async def test_unconfigured_bot_is_refused_without_a_call() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(bot="someone-else"))
    assert result.outcome is ProviderOutcome.NOT_SENT and not result.retryable
    assert result.failure_code is FailureCode.PROVIDER_CONFIG
    assert rec.requests == []


@pytest.mark.parametrize("field", [{"token": FAKE_TOKEN}, {"url": "https://evil.example"},
                                   {"bot_token": FAKE_TOKEN}, {"api_base_url": "https://x"}])
def test_requests_cannot_carry_tokens_or_urls(field: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        req(**field)


@pytest.mark.parametrize("base", ["http://api.telegram.org", "https://api.telegram.org/x",
                                  "https://u:p@api.telegram.org", "https://h?q=1", "ftp://h"])
def test_base_url_must_be_plain_https_origin(base: str) -> None:
    with pytest.raises(ValueError):
        TelegramAdapter(registry(), httpx.AsyncClient(), api_base_url=base)


async def test_wrong_channel_request_is_refused_without_a_call() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    sms = SmsSendRequest(message_id=uuid.uuid4(), attempt=1, recipient="+251912000001",
                         text="hi")
    result = await adapter.send(sms)
    assert result.outcome is ProviderOutcome.NOT_SENT
    assert result.failure_code is FailureCode.PROVIDER_CONFIG
    assert rec.requests == []


async def test_expired_request_is_not_sent() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(expires_at=NOW - timedelta(seconds=1)))
    assert result.outcome is ProviderOutcome.NOT_SENT and not result.retryable
    assert result.failure_code is FailureCode.EXPIRED_BEFORE_SEND
    assert rec.requests == []


@pytest.mark.parametrize("chat_id", [0, 2**52, -(2**52)])
async def test_invalid_chat_id_rejected_without_a_call(chat_id: int) -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(chat_id=chat_id))
    assert result.outcome is ProviderOutcome.REJECTED
    assert result.failure_code is FailureCode.CHAT_NOT_FOUND
    assert rec.requests == []


# ------------------------------------------------------------------ Unicode and length

AMHARIC = "ሀ"
EMOJI = "😀"  # astral: 2 UTF-16 units


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("a" * 4096, True),
        ("a" * 4097, False),
        (AMHARIC * 4096, True),
        (AMHARIC * 4097, False),
        (EMOJI * 2048, True),  # 4096 units
        (EMOJI * 2048 + "a", False),  # 4097 units
        ("a" * 4095 + EMOJI, False),  # 4097 units though only 4096 code points
        ("እንደምን አደርሩ! 🌱 Ünïcödé — 中文\nline two", True),
    ],
)
async def test_unicode_length_limit_in_utf16_units(text: str, accepted: bool) -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(text=text))
    if accepted:
        assert result.outcome is ProviderOutcome.SENT
        assert json.loads(rec.requests[0].content)["text"] == text  # exact round trip
    else:
        assert result.outcome is ProviderOutcome.REJECTED
        assert result.failure_code is FailureCode.TEXT_REJECTED
        assert rec.requests == []


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "bell\x07", "nul\x00"])
async def test_blank_or_control_text_rejected_without_a_call(text: str) -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    result = await adapter.send(req(text=text))
    assert result.outcome is ProviderOutcome.REJECTED
    assert result.failure_code is FailureCode.TEXT_REJECTED
    assert rec.requests == []


async def test_no_parse_mode_so_markup_is_sent_literally() -> None:
    adapter, rec = adapter_for(respond(200, ok_body()))
    await adapter.send(req(text="<b>*not bold*</b> _x_"))
    body = json.loads(rec.requests[0].content)
    assert "parse_mode" not in body and body["text"] == "<b>*not bold*</b> _x_"


# ------------------------------------------------------------------------------ 429


@pytest.mark.parametrize(
    ("params", "expected"),
    [({"retry_after": 17}, 17), ({"retry_after": 10**12}, 3600),
     ({"retry_after": 1.2}, 2), ({"retry_after": 0}, 0),
     ({}, None), ({"retry_after": -5}, None), ({"retry_after": "9"}, None),
     ({"retry_after": True}, None)],
)
async def test_429_is_not_sent_with_retry_after(params: dict[str, Any],
                                                expected: int | None) -> None:
    adapter, rec = adapter_for(
        respond(429, tg_error(429, "Too Many Requests: retry after 17", **params))
    )
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.NOT_SENT and result.retryable
    assert result.failure_code is FailureCode.RATE_LIMITED
    assert result.retry_after_seconds == expected
    assert result.provider_status == "tg_429"
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.ACCEPTED  # worker reschedules
    assert len(rec.requests) == 1  # the adapter never sleeps or retries


async def test_429_with_non_json_body_still_not_sent() -> None:
    adapter, _ = adapter_for(respond(429, raw=b"<html>slow down</html>"))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.NOT_SENT and result.retry_after_seconds is None


# ------------------------------------------------------- blocked bot and invalid chat


@pytest.mark.parametrize(
    ("status", "description", "code"),
    [
        (403, "Forbidden: bot was blocked by the user", FailureCode.RECIPIENT_BLOCKED_BOT),
        (403, "Forbidden: bot was kicked from the group chat", FailureCode.RECIPIENT_BLOCKED_BOT),
        (403, "Forbidden: bot can't initiate conversation with a user", FailureCode.CHAT_NOT_FOUND),
        (403, "Forbidden: user is deactivated", FailureCode.CHAT_NOT_FOUND),
        (403, "Forbidden: something new", FailureCode.PROVIDER_REJECTED),
        (400, "Bad Request: chat not found", FailureCode.CHAT_NOT_FOUND),
        (400, "Bad Request: PEER_ID_INVALID", FailureCode.CHAT_NOT_FOUND),
        (400, "Bad Request: group chat was upgraded to a supergroup chat",
         FailureCode.CHAT_NOT_FOUND),
        (400, "Bad Request: message is too long", FailureCode.TEXT_REJECTED),
        (400, "Bad Request: message text is empty", FailureCode.TEXT_REJECTED),
        (400, "Bad Request: unexpected", FailureCode.PROVIDER_REJECTED),
        (409, "Conflict: whatever", FailureCode.PROVIDER_REJECTED),
    ],
)
async def test_definite_rejections(status: int, description: str, code: FailureCode) -> None:
    adapter, rec = adapter_for(respond(status, tg_error(status, description)))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.REJECTED and not result.retryable
    assert result.failure_code is code
    assert result.provider_status == f"tg_{status}"
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.FAILED
    assert len(rec.requests) == 1
    assert_no_leak(result, description)  # Telegram's text never copied into results


async def test_rejection_with_non_json_body() -> None:
    adapter, _ = adapter_for(respond(400, raw=b"not json"))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.REJECTED
    assert result.failure_code is FailureCode.PROVIDER_REJECTED


@pytest.mark.parametrize("status", [401, 404])
async def test_bad_token_is_not_sent_and_not_retryable(status: int) -> None:
    adapter, _ = adapter_for(respond(status, tg_error(status, "Unauthorized")))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.NOT_SENT and not result.retryable
    assert result.failure_code is FailureCode.PROVIDER_AUTH


# ----------------------------------------------------- ambiguous and malformed responses


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_errors_are_unknown_and_never_retried(status: int) -> None:
    adapter, rec = adapter_for(respond(status, tg_error(status, "Internal Server Error")))
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert result.failure_code is FailureCode.PROVIDER_SERVER_ERROR
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.UNKNOWN
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    "handler",
    [
        respond(200, raw=b"<html>proxy</html>"),
        respond(200, raw=b""),
        respond(200, [1, 2]),
        respond(200, {"result": ok_body()["result"]}),  # ok missing
        respond(200, {"ok": False, "error_code": 400, "description": "x"}),
        respond(200, {"ok": True}),  # result missing
        respond(200, ok_body(message_id=None)),
        respond(200, ok_body(message_id="4711")),
        respond(200, ok_body(message_id=0)),
        respond(200, ok_body(message_id=-3)),
        respond(200, ok_body(message_id=True)),
        respond(200, {"ok": True, "result": {"message_id": 5}}),  # chat missing
        respond(200, ok_body(chat_id=CHAT + 1)),  # different chat
        respond(200, ok_body(chat_id=str(CHAT))),
        respond(400, ok_body()),  # ok:true with an error status
        respond(302, {"ok": True}),
    ],
    ids=["html", "empty", "array", "no-ok", "ok-false-200", "no-result", "no-id", "str-id",
         "zero-id", "neg-id", "bool-id", "no-chat", "other-chat", "str-chat", "ok-on-400",
         "redirect"],
)
async def test_malformed_or_inconsistent_responses_are_unknown(handler: Handler) -> None:
    adapter, rec = adapter_for(handler)
    result = await adapter.send(req())
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert result.failure_code is FailureCode.MALFORMED_RESPONSE
    assert result.provider_message_id is None
    assert len(rec.requests) == 1


# ------------------------------------------------------------- redirects (MSG-04 P1)

REDIRECT_TARGET = "https://redirect-target.invalid/collect"
REDIRECT_STATUSES = [301, 302, 303, 307, 308]


def redirecting(status: int, location: str) -> Handler:
    """Telegram origin answers with a redirect; any other host answers 'success'."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegram.org" and request.url.path.endswith("/sendMessage") \
                and "followed" not in request.url.path:
            return httpx.Response(status, headers={"Location": location},
                                  json={"ok": True, "result": ok_body()["result"]})
        return httpx.Response(200, json=ok_body())
    return handler


def redirect_enabled_adapter(handler: Handler) -> tuple[TelegramAdapter, Recorder]:
    rec = Recorder(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec), follow_redirects=True)
    assert client.follow_redirects is True  # an unsafe injected client
    return TelegramAdapter(registry(), client, clock=lambda: NOW), rec


async def test_redirect_mock_is_meaningful() -> None:
    """Without the adapter's per-request override, this client would follow the redirect."""
    rec = Recorder(redirecting(307, REDIRECT_TARGET))
    async with httpx.AsyncClient(transport=httpx.MockTransport(rec),
                                 follow_redirects=True) as client:
        await client.post(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
                          json={"chat_id": CHAT, "text": "x"})
    assert [r.url.host for r in rec.requests] == ["api.telegram.org", "redirect-target.invalid"]


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
@pytest.mark.parametrize(
    "location",
    [REDIRECT_TARGET,
     f"https://redirect-target.invalid/bot{FAKE_TOKEN}/sendMessage",
     "/followed/sendMessage",  # same origin, different path
     f"https://api.telegram.org/bot{OTHER_TOKEN}/followed/sendMessage"],
    ids=["other-host", "other-host-token-path", "relative", "same-host-other-bot"],
)
async def test_redirects_are_never_followed_even_if_client_allows(
    status: int, location: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter, rec = redirect_enabled_adapter(redirecting(status, location))
    text = "private test message 482913"
    result = await adapter.send(req(text=text))
    # Exactly one request, to the configured origin; the target is never contacted.
    assert len(rec.requests) == 1
    assert rec.requests[0].url.host == "api.telegram.org"
    assert all(r.url.host != "redirect-target.invalid" for r in rec.requests)
    # Ambiguous, never sent, never retried automatically.
    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.failure_code is FailureCode.MALFORMED_RESPONSE
    assert result.retryable is False and result.retry_after_seconds is None
    assert result.provider_message_id is None
    assert result.provider_status == f"tg_{status}"
    assert OUTCOME_TO_STATUS[result.outcome] is MessageStatus.UNKNOWN
    # Location is neither recorded nor logged.
    assert_no_leak(result, "redirect-target", "followed", text, str(CHAT))
    assert "redirect-target" not in caplog.text and FAKE_TOKEN not in caplog.text


async def test_factory_client_also_never_follows_redirects() -> None:
    rec = Recorder(redirecting(308, REDIRECT_TARGET))
    client = make_client()
    client._transport = httpx.MockTransport(rec)  # test-only: keep factory settings
    try:
        adapter = TelegramAdapter(registry(), client, clock=lambda: NOW)
        result = await adapter.send(req())
    finally:
        await client.aclose()
    assert len(rec.requests) == 1 and result.outcome is ProviderOutcome.UNKNOWN


def raising(exc: BaseException) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return handler


def _ssl_connect_error() -> httpx.ConnectError:
    err = httpx.ConnectError("certificate verify failed")
    err.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    return err


@pytest.mark.parametrize(
    ("exc", "outcome", "code", "retryable"),
    [
        (httpx.ConnectError("refused"), ProviderOutcome.NOT_SENT,
         FailureCode.PROVIDER_UNAVAILABLE, True),
        (httpx.ConnectTimeout("connect timeout"), ProviderOutcome.NOT_SENT,
         FailureCode.PROVIDER_UNAVAILABLE, True),
        (httpx.PoolTimeout("pool"), ProviderOutcome.NOT_SENT,
         FailureCode.PROVIDER_UNAVAILABLE, True),
        (_ssl_connect_error(), ProviderOutcome.NOT_SENT, FailureCode.PROVIDER_CONFIG, False),
        (httpx.ReadTimeout("read timeout"), ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT, False),
        (httpx.WriteTimeout("write timeout"), ProviderOutcome.UNKNOWN, FailureCode.TIMEOUT,
         False),
        (httpx.RemoteProtocolError("server disconnected"), ProviderOutcome.UNKNOWN,
         FailureCode.MALFORMED_RESPONSE, False),
        (httpx.ReadError("reset"), ProviderOutcome.UNKNOWN, FailureCode.MALFORMED_RESPONSE,
         False),
    ],
    ids=["refused", "connect-timeout", "pool-timeout", "tls", "read-timeout",
         "write-timeout", "disconnect", "read-error"],
)
async def test_transport_failures(exc: BaseException, outcome: ProviderOutcome,
                                  code: FailureCode, retryable: bool) -> None:
    adapter, rec = adapter_for(raising(exc))
    result = await adapter.send(req())
    assert (result.outcome, result.failure_code, result.retryable) == (outcome, code, retryable)
    assert len(rec.requests) == 1  # one attempt, no internal retry
    assert_no_leak(result)


async def test_unexpected_exception_is_unknown_and_does_not_leak_url() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"exploded while calling {request.url}")

    adapter, _ = adapter_for(boom)
    result = await adapter.send(req())  # must not raise
    assert result.outcome is ProviderOutcome.UNKNOWN and not result.retryable
    assert result.failure_code is FailureCode.INTERRUPTED
    assert result.detail == "unexpected RuntimeError"
    assert_no_leak(result)


# ------------------------------------------------------------------- token redaction


@pytest.fixture
def capture_all(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "oan_messaging.adapters.telegram"):
        caplog.set_level(logging.DEBUG, logger=name)
    yield caplog


@pytest.mark.parametrize(
    "handler",
    [respond(200, ok_body()), respond(401, tg_error(401, "Unauthorized")),
     respond(429, tg_error(429, "Too Many Requests", retry_after=3)),
     respond(503, raw=b"down"), raising(httpx.ReadTimeout("t")),
     raising(RuntimeError(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"))],
    ids=["ok", "401", "429", "503", "timeout", "unexpected"],
)
async def test_logs_never_contain_token_text_or_chat(
    handler: Handler, capture_all: pytest.LogCaptureFixture
) -> None:
    adapter, _ = adapter_for(handler)
    text = "secret-ish message body 482913"
    result = await adapter.send(req(text=text))
    logged = capture_all.text + "".join(str(r.args) for r in capture_all.records)
    for needle in (FAKE_TOKEN, "bot123456789:", text, str(CHAT)):
        assert needle not in logged
    assert any(r.name == "oan_messaging.adapters.telegram" for r in capture_all.records)
    assert_no_leak(result, text, str(CHAT))


async def test_httpx_request_url_log_is_redacted(capture_all: pytest.LogCaptureFixture) -> None:
    adapter, _ = adapter_for(respond(200, ok_body()))
    await adapter.send(req())
    httpx_lines = [r.getMessage() for r in capture_all.records if r.name == "httpx"]
    # httpx really does log the request URL; prove the test is meaningful.
    assert any("HTTP Request: POST" in m for m in httpx_lines)
    assert all(FAKE_TOKEN not in m for m in httpx_lines)
    assert any("/bot<redacted>/sendMessage" in m for m in httpx_lines)


@pytest.mark.parametrize(
    "raw",
    [f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
     f"token={FAKE_TOKEN}", f"{FAKE_TOKEN}", f"'bot{FAKE_TOKEN}'"],
)
def test_redact_tokens(raw: str) -> None:
    out = redact_tokens(raw)
    assert FAKE_TOKEN not in out and "AAFakeToken" not in out and "<redacted>" in out


def test_redact_explicit_secret_even_if_unusual_shape() -> None:
    assert redact_tokens("x odd-secret y", ("odd-secret",)) == "x <redacted> y"


def test_adapter_and_registry_reprs_hide_tokens() -> None:
    adapter, _ = adapter_for(respond(200, ok_body()))
    reg = registry()
    for shown in (repr(adapter), str(adapter), repr(reg), repr(reg.token_for("grievance"))):
        assert FAKE_TOKEN not in shown and OTHER_TOKEN not in shown


# --------------------------------------------------------------- secret configuration


def test_registry_from_environment() -> None:
    env = {
        f"{TOKEN_ENV_PREFIX}GRIEVANCE": FAKE_TOKEN,
        f"{TOKEN_ENV_PREFIX}ADVISORY": f"  {OTHER_TOKEN}\n",
        f"{TOKEN_ENV_PREFIX}EMPTY": "  ",
        "UNRELATED": FAKE_TOKEN,
    }
    reg = TelegramBotRegistry.from_environ(env)
    assert reg.keys() == frozenset({"grievance", "advisory"})
    token = reg.token_for("advisory")
    assert token is not None and token.get_secret_value() == OTHER_TOKEN


@pytest.mark.parametrize("bad", ["not-a-token", "123:short", f"{FAKE_TOKEN} extra",
                                 "abc:AAFakeTokenForTestsOnly_abcdefghijk"])
def test_invalid_token_rejected_without_echo(bad: str) -> None:
    with pytest.raises(ValueError) as info:
        TelegramBotRegistry({"grievance": SecretStr(bad)})
    assert bad not in str(info.value)


@pytest.mark.parametrize("key", ["", "Upper", "has space", "x" * 65])
def test_invalid_bot_key_rejected(key: str) -> None:
    with pytest.raises(ValueError):
        TelegramBotRegistry({key: SecretStr(FAKE_TOKEN)})


async def test_recommended_client_settings() -> None:
    client = make_client()
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout.connect == 5.0 and client.timeout.read == 15.0
    finally:
        await client.aclose()
