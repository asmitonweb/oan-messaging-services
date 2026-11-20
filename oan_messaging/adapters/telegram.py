"""Telegram Bot API transport (MSG-04) implementing the frozen ``MessageAdapter`` contract.

Scope: outbound plain-text ``sendMessage`` only. No inbound updates, webhooks, onboarding
or conversations.

Recipients must have started the bot first. Telegram does not let a bot open a
conversation with a user. Until the user sends ``/start`` (or any message) to the bot,
``sendMessage`` fails with 403 "bot can't initiate conversation" or 400 "chat not found".
This adapter reports that as a definite rejection (``chat_not_found``). Obtaining and
storing chat IDs belongs to the onboarding flow, which is out of scope here.

Outcome semantics:

* A Bot API ``ok: true`` with a ``message_id`` is **sent**: Telegram accepted the message
  for the chat. It is never a handset-delivery claim, and Telegram never reaches
  ``delivered`` (``CHANNEL_DELIVERY_RECEIPTS[TELEGRAM] is False``).
* 429 flood control is **not_sent** with ``retry_after_seconds``. Telegram rejected the
  request. This adapter never sleeps or retries; the worker schedules the retry.
* Blocked bot, unknown chat and other 4xx are definite **rejected** outcomes.
* A timeout after the request may have been written, a 5xx, or a malformed or
  inconsistent success body is **unknown**. It is never retryable, so there is no
  blind resend.

Security:

* Exactly one HTTP request per ``send``. Redirect following is disabled on every request,
  even if the injected client enables it; a 3xx is ``unknown`` and the target is never
  contacted.
* Bot tokens come only from secret configuration (``TelegramBotRegistry``). Requests
  carry a configured bot *key*. A request can never supply a token or a URL, and the
  API base URL is fixed deployment configuration.
* The token is part of the Bot API URL path. It is never logged, never placed in a
  ``ProviderResult`` and never raised. A logging filter redacts any token-shaped string
  in ``httpx``/``httpcore`` records, which would otherwise log the full request URL.
* Results and logs never contain message text, chat IDs, tokens or response bodies.
"""

from __future__ import annotations

import logging
import math
import re
import ssl
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from oan_messaging.contracts import (
    MAX_RETRY_AFTER_SECONDS,
    TELEGRAM_MAX_TEXT_UTF16_UNITS,
    Channel,
    FailureCode,
    Provider,
    ProviderOutcome,
    ProviderResult,
    SendRequest,
    TelegramSendRequest,
)

__all__ = [
    "DEFAULT_API_BASE_URL",
    "TOKEN_ENV_PREFIX",
    "TelegramAdapter",
    "TelegramBotRegistry",
    "install_token_redaction",
    "make_client",
    "redact_tokens",
]

log = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://api.telegram.org"
#: ``OAN_MSG_TELEGRAM_BOT_TOKEN__<BOT_KEY>=<token>``, e.g. ``..._TOKEN__GRIEVANCE``.
TOKEN_ENV_PREFIX = "OAN_MSG_TELEGRAM_BOT_TOKEN__"  # noqa: S105 (env var name, not a secret)

BOT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Bot API token shape: ``<bot id>:<secret>``.
TOKEN_PATTERN = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{30,64}$")
#: Anything token-shaped, with or without the ``bot`` URL prefix.
_TOKEN_SEARCH = re.compile(r"(?<![A-Za-z0-9])(bot)?[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")
_REDACTED = "bot<redacted>"

_CHAT_ID_LIMIT = 2**52


def redact_tokens(text: str, extra: tuple[str, ...] = ()) -> str:
    """Replace every token-shaped substring (and any explicitly known token)."""
    for secret in extra:
        if secret:
            text = text.replace(secret, "<redacted>")
    return _TOKEN_SEARCH.sub(_REDACTED, text)


class _TokenRedactingFilter(logging.Filter):
    """Rewrites log records so a Bot API URL can never reveal its token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed record: drop args rather than risk a leak
            message = str(record.msg)
        redacted = redact_tokens(message)
        if redacted != message or record.args:
            record.msg = redacted
            record.args = None
        return True


_FILTER = _TokenRedactingFilter()
_REDACTED_LOGGERS = ("httpx", "httpcore", __name__)


def install_token_redaction() -> None:
    """Attach the redaction filter to the HTTP client and adapter loggers (idempotent).

    A logger filter applies to records created on that logger. httpx logs request URLs
    on the ``httpx`` logger, and httpcore logs on its own ``httpcore.*`` children,
    so both are covered.
    """
    from oan_messaging.redaction import install_redaction

    install_redaction()
    names = list(_REDACTED_LOGGERS) + [
        n for n in logging.root.manager.loggerDict if n.startswith(("httpx.", "httpcore."))
    ]
    for name in names:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)


class TelegramBotRegistry:
    """Configured bot identities: bot key -> secret token. Built only from secret config."""

    def __init__(self, tokens: Mapping[str, SecretStr]) -> None:
        cleaned: dict[str, SecretStr] = {}
        for key, token in tokens.items():
            if not BOT_KEY_PATTERN.fullmatch(key):
                raise ValueError("invalid Telegram bot key")
            if not TOKEN_PATTERN.fullmatch(token.get_secret_value()):
                # Never include the value (or the key, which may hint at it) in the error.
                raise ValueError("configured Telegram bot token has an invalid format")
            cleaned[key] = token
        self._tokens = cleaned

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> TelegramBotRegistry:
        """Read ``OAN_MSG_TELEGRAM_BOT_TOKEN__<KEY>`` variables (vault-injected env)."""
        tokens = {
            name[len(TOKEN_ENV_PREFIX):].lower(): SecretStr(value.strip())
            for name, value in environ.items()
            if name.startswith(TOKEN_ENV_PREFIX) and value.strip()
        }
        return cls(tokens)

    def keys(self) -> frozenset[str]:
        return frozenset(self._tokens)

    def token_for(self, bot: str) -> SecretStr | None:
        return self._tokens.get(bot)

    def secrets(self) -> tuple[str, ...]:
        return tuple(t.get_secret_value() for t in self._tokens.values())

    def __repr__(self) -> str:
        return f"TelegramBotRegistry(bots={sorted(self._tokens)})"


def make_client(
    *, connect_timeout: float = 5.0, read_timeout: float = 15.0
) -> httpx.AsyncClient:
    """Recommended client: server certificates verified, no redirects, no env proxies.

    Callers own the client lifecycle (``await client.aclose()``).
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
        verify=True,
        follow_redirects=False,
        trust_env=False,
    )


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


_CONTROL = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]")


def _has_ssl_cause(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, ssl.SSLError):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


class TelegramAdapter:
    """``MessageAdapter`` for the Telegram Bot API ``sendMessage`` method."""

    provider: ClassVar[Provider] = Provider.TELEGRAM
    channel: ClassVar[Channel] = Channel.TELEGRAM

    def __init__(
        self,
        registry: TelegramBotRegistry,
        client: httpx.AsyncClient,
        *,
        api_base_url: str = DEFAULT_API_BASE_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        parts = urlsplit(api_base_url)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("api_base_url must be an https origin from configuration")
        if parts.path not in ("", "/") or parts.username or parts.password:
            raise ValueError("api_base_url must not contain a path or credentials")
        self._base = f"https://{parts.netloc}"
        self._registry = registry
        self._client = client
        self._clock = clock
        install_token_redaction()

    def __repr__(self) -> str:
        return f"TelegramAdapter(base={self._base!r}, bots={sorted(self._registry.keys())})"

    # -- public contract -------------------------------------------------------------

    async def send(self, request: SendRequest) -> ProviderResult:
        started = self._clock()
        try:
            result = await self._send(request, started)
        except Exception as exc:  # contract: never raise; never leak URL/token via str(exc)
            result = self._result(
                started, ProviderOutcome.UNKNOWN, "adapter_error",
                failure=FailureCode.INTERRUPTED, detail=f"unexpected {type(exc).__name__}",
            )
        self._log(request, result)
        return result

    # -- internals -------------------------------------------------------------------

    async def _send(self, request: SendRequest, started: datetime) -> ProviderResult:
        if not isinstance(request, TelegramSendRequest):
            return self._result(started, ProviderOutcome.NOT_SENT, "wrong_channel",
                                failure=FailureCode.PROVIDER_CONFIG,
                                detail="non-telegram request routed to telegram adapter")
        token = self._registry.token_for(request.bot)
        if token is None:
            # Unknown or unauthorized bot identity: never fall back to another bot.
            return self._result(started, ProviderOutcome.NOT_SENT, "bot_not_configured",
                                failure=FailureCode.PROVIDER_CONFIG,
                                detail="bot key is not configured")
        if request.chat_id == 0 or abs(request.chat_id) >= _CHAT_ID_LIMIT:
            return self._result(started, ProviderOutcome.REJECTED, "invalid_chat_id",
                                failure=FailureCode.CHAT_NOT_FOUND,
                                detail="chat_id outside the Telegram range")
        text = request.text
        if not text.strip() or _CONTROL.search(text):
            return self._result(started, ProviderOutcome.REJECTED, "invalid_text",
                                failure=FailureCode.TEXT_REJECTED,
                                detail="text is blank or has control characters")
        if _utf16_units(text) > TELEGRAM_MAX_TEXT_UTF16_UNITS:
            return self._result(started, ProviderOutcome.REJECTED, "text_too_long",
                                failure=FailureCode.TEXT_REJECTED,
                                detail="text exceeds 4096 UTF-16 units")
        if request.expires_at is not None and request.expires_at <= started:
            return self._result(started, ProviderOutcome.NOT_SENT, "expired",
                                failure=FailureCode.EXPIRED_BEFORE_SEND,
                                detail="expired before the provider call")

        url = f"{self._base}/bot{token.get_secret_value()}/sendMessage"
        payload: dict[str, Any] = {"chat_id": request.chat_id, "text": text}
        try:
            # Enforced per request, whatever the injected client's default: a redirect
            # must never forward the token-bearing URL or the message to another origin.
            response = await self._client.post(url, json=payload, follow_redirects=False)
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            # No connection was established, so the request was never written.
            return self._result(started, ProviderOutcome.NOT_SENT, "connect_timeout",
                                failure=FailureCode.PROVIDER_UNAVAILABLE, retryable=True,
                                detail="connection not established")
        except httpx.ConnectError as exc:
            if _has_ssl_cause(exc):
                return self._result(started, ProviderOutcome.NOT_SENT, "tls_error",
                                    failure=FailureCode.PROVIDER_CONFIG,
                                    detail="TLS handshake failed")
            return self._result(started, ProviderOutcome.NOT_SENT, "conn_refused",
                                failure=FailureCode.PROVIDER_UNAVAILABLE, retryable=True,
                                detail="connection refused or DNS failure")
        except httpx.TimeoutException:
            # Read/write timeout: the request may have reached Telegram.
            return self._result(started, ProviderOutcome.UNKNOWN, "timeout",
                                failure=FailureCode.TIMEOUT, detail="no response in time")
        except httpx.HTTPError as exc:
            return self._result(started, ProviderOutcome.UNKNOWN, "transport_error",
                                failure=FailureCode.MALFORMED_RESPONSE,
                                detail=f"transport {type(exc).__name__}")
        return self._classify(started, request, response)

    def _classify(
        self, started: datetime, request: TelegramSendRequest, response: httpx.Response
    ) -> ProviderResult:
        status = response.status_code
        tag = f"tg_{status}"
        body = _json_object(response)

        if 500 <= status <= 599:
            return self._result(started, ProviderOutcome.UNKNOWN, tag,
                                failure=FailureCode.PROVIDER_SERVER_ERROR,
                                detail="Telegram server error")
        if status == 200:
            return self._classify_success(started, request, body, tag)
        if body is not None and body.get("ok") is True:
            return self._result(started, ProviderOutcome.UNKNOWN, tag,
                                failure=FailureCode.MALFORMED_RESPONSE,
                                detail="ok:true with non-200 status")
        if status == 429:
            return self._result(started, ProviderOutcome.NOT_SENT, tag,
                                failure=FailureCode.RATE_LIMITED, retryable=True,
                                retry_after=_retry_after(body),
                                detail="flood control")
        if status in (401, 404):
            # Telegram answers 401 Unauthorized / 404 Not Found for a bad or revoked token.
            return self._result(started, ProviderOutcome.NOT_SENT, tag,
                                failure=FailureCode.PROVIDER_AUTH,
                                detail="bot token rejected")
        if 400 <= status <= 499:
            failure, detail = _classify_rejection(status, body)
            return self._result(started, ProviderOutcome.REJECTED, tag,
                                failure=failure, detail=detail)
        # 1xx/3xx. Redirects are never followed (one call, configured origin only). The
        # Location target is not contacted or recorded. Cannot prove what happened.
        return self._result(started, ProviderOutcome.UNKNOWN, tag,
                            failure=FailureCode.MALFORMED_RESPONSE,
                            detail="unexpected HTTP status")

    def _classify_success(
        self, started: datetime, request: TelegramSendRequest,
        body: dict[str, Any] | None, tag: str,
    ) -> ProviderResult:
        result = body.get("result") if body is not None else None
        if body is None or body.get("ok") is not True or not isinstance(result, dict):
            return self._result(started, ProviderOutcome.UNKNOWN, tag,
                                failure=FailureCode.MALFORMED_RESPONSE,
                                detail="success body missing ok/result")
        message_id = result.get("message_id")
        if type(message_id) is not int or message_id <= 0:
            return self._result(started, ProviderOutcome.UNKNOWN, tag,
                                failure=FailureCode.MALFORMED_RESPONSE,
                                detail="success body missing message_id")
        chat = result.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if type(chat_id) is not int or chat_id != request.chat_id:
            return self._result(started, ProviderOutcome.UNKNOWN, tag,
                                failure=FailureCode.MALFORMED_RESPONSE,
                                detail="success body chat does not match request")
        return self._result(started, ProviderOutcome.SENT, tag,
                            provider_message_id=str(message_id))

    def _result(
        self,
        started: datetime,
        outcome: ProviderOutcome,
        provider_status: str,
        *,
        failure: FailureCode | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
        provider_message_id: str | None = None,
        detail: str | None = None,
    ) -> ProviderResult:
        finished = self._clock()
        secrets = self._registry.secrets()
        return ProviderResult(
            outcome=outcome,
            provider=Provider.TELEGRAM,
            provider_message_id=provider_message_id,
            provider_status=redact_tokens(provider_status, secrets)[:64],
            failure_code=failure,
            retryable=retryable,
            retry_after_seconds=retry_after,
            started_at=started,
            finished_at=max(finished, started),
            detail=None if detail is None else redact_tokens(detail, secrets)[:200],
        )

    def _log(self, request: SendRequest, result: ProviderResult) -> None:
        log.info(
            "telegram send message_id=%s attempt=%s outcome=%s provider_status=%s "
            "failure_code=%s retry_after=%s",
            request.message_id, request.attempt, result.outcome.value,
            result.provider_status,
            result.failure_code.value if result.failure_code else None,
            result.retry_after_seconds,
        )


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _retry_after(body: dict[str, Any] | None) -> int | None:
    params = body.get("parameters") if body is not None else None
    value = params.get("retry_after") if isinstance(params, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(math.ceil(value), MAX_RETRY_AFTER_SECONDS)


def _classify_rejection(status: int, body: dict[str, Any] | None) -> tuple[FailureCode, str]:
    """Map a definite 4xx rejection. Only fixed strings leave this function."""
    description = body.get("description") if body is not None else None
    text = description.lower() if isinstance(description, str) else ""
    if status == 403:
        if "blocked by the user" in text or "kicked" in text:
            return FailureCode.RECIPIENT_BLOCKED_BOT, "bot blocked or removed by recipient"
        if "initiate conversation" in text:
            return FailureCode.CHAT_NOT_FOUND, "recipient has not started the bot"
        if "deactivated" in text:
            return FailureCode.CHAT_NOT_FOUND, "recipient account deactivated"
        return FailureCode.PROVIDER_REJECTED, "forbidden by Telegram"
    if "chat not found" in text or "peer_id_invalid" in text or "user not found" in text:
        return FailureCode.CHAT_NOT_FOUND, "chat not found"
    if "upgraded to a supergroup" in text:
        return FailureCode.CHAT_NOT_FOUND, "chat migrated to a supergroup"
    if "message is too long" in text or "text must be non-empty" in text or (
        "message text is empty" in text
    ):
        return FailureCode.TEXT_REJECTED, "text rejected by Telegram"
    return FailureCode.PROVIDER_REJECTED, "request rejected by Telegram"
