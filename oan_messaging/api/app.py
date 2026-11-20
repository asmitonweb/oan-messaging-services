"""FastAPI application: authenticated submission and status endpoints (MSG-02).

Request handlers never call a provider: they authenticate, validate, authorize, persist
(committed before 202) and return. Delivery happens in MSG-05 workers.

Order of checks for POST: credential (401) -> channel permission (403) -> media type
(415) -> size (413) -> Idempotency-Key header (422) -> body (422) -> purpose / bot
permission (403) -> idempotent persist (202 / 200 / 409 / 422 expiry). Any failure
whose outcome is uncertain answers 503: the caller must retry with the same key and
unchanged body (the message may already be stored).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from oan_messaging.api.problems import problem, validation_items
from oan_messaging.contracts import (
    CONTRACT_VERSION,
    IDEMPOTENCY_KEY_PATTERN,
    CallerContext,
    Channel,
    ErrorItem,
    IdempotencyConflict,
    IdempotencyDecision,
    MessageStatusView,
    SmsSubmission,
    StoredMessage,
    Submission,
    SubmissionResponse,
    TelegramSubmission,
    to_status_view,
)
from oan_messaging.storage.errors import BindingIntegrityError, ExpiryRejected

log = logging.getLogger("oan_messaging.api")

_BEARER = re.compile(r"^Bearer ([^\s]+)$", re.IGNORECASE)
_JSON_MEDIA = re.compile(r"^application/json\s*(;\s*charset=utf-8\s*)?$", re.IGNORECASE)


class AcceptanceStore(Protocol):
    async def create_or_get(
        self,
        caller: CallerContext,
        idempotency_key: str,
        submission: Submission,
        now: datetime,
    ) -> tuple[StoredMessage, IdempotencyDecision]: ...

    async def get_for_caller(
        self, caller_id: str, message_id: uuid.UUID
    ) -> StoredMessage | None: ...


@dataclass
class ApiDependencies:
    repository: AcceptanceStore
    authenticate: Callable[[str], Awaitable[CallerContext | None]]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    max_body_bytes: int = 16 * 1024
    broadcast_enabled: bool = True  # injected harnesses may exercise future coverage
    #: GET /api/v1/charts (statistics for the OAN dashboards); None = not available.
    stats: Any = None


def _submission_response(m: StoredMessage) -> dict[str, Any]:
    return SubmissionResponse(
        id=m.id,
        idempotency_key=m.idempotency_key,
        channel=m.channel,
        status=m.status,
        source_reference=m.source_reference,
        created_at=m.created_at,
        expires_at=m.expires_at,
    ).model_dump(mode="json", exclude_none=True)


def create_app(deps: ApiDependencies | None = None) -> FastAPI:
    """Build the app. Without ``deps`` the lifespan wires PostgreSQL from environment
    settings and fails startup on invalid secrets or policy (e.g. non-positive durations)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            app.state.deps = deps
            yield
            return
        from oan_messaging.storage.config import ServiceSecrets, StorageSettings, load_policy
        from oan_messaging.storage.credentials import authenticate
        from oan_messaging.storage.db import make_engine, make_session_factory
        from oan_messaging.storage.repository import PostgresMessageRepository

        settings = StorageSettings()
        policy = load_policy(settings)
        secrets = ServiceSecrets.from_settings(settings)
        engine = make_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
        sf = make_session_factory(engine)
        repo = PostgresMessageRepository(
            sf, cipher=secrets.cipher, fingerprinter=secrets.fingerprinter, policy=policy
        )

        async def auth(token: str) -> CallerContext | None:
            return await authenticate(sf, secrets.credential_pepper, token)

        from oan_messaging.stats.charts import StatsService

        app.state.deps = ApiDependencies(
            repository=repo, authenticate=auth, max_body_bytes=settings.max_body_bytes,
            broadcast_enabled=settings.broadcast_enabled, stats=StatsService(sf),
        )
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="OAN Messaging Service",
        version=CONTRACT_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if deps is not None:
        app.state.deps = deps

    # ------------------------------------------------------------ cross-cutting

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.monotonic()
        response = await call_next(request)
        route = request.scope.get("route")
        log.info(
            "request method=%s route=%s status=%s caller=%s duration_ms=%.1f",
            request.method,
            getattr(route, "path", "<unmatched>"),
            response.status_code,
            getattr(request.state, "caller_id", None),
            (time.monotonic() - started) * 1000,
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 405:
            return problem(405, "validation_failed", [ErrorItem(code="method_not_allowed")])
        return problem(404, "not_found")

    @app.exception_handler(RequestValidationError)
    async def request_validation(_r: Request, _exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation_failed", [ErrorItem(code="invalid_request")])

    @app.exception_handler(Exception)
    async def unexpected(_r: Request, exc: Exception) -> JSONResponse:
        # Exception text may contain SQL or values: log the type only.
        log.error("unhandled error type=%s", type(exc).__name__)
        return problem(
            503, "service_unavailable", [ErrorItem(code="outcome_uncertain_retry_same_key")]
        )

    # ------------------------------------------------------------------ helpers

    async def caller_of(request: Request) -> CallerContext | None:
        header = request.headers.get("authorization", "")
        match = _BEARER.fullmatch(header.strip())
        if match is None:
            return None
        d: ApiDependencies = request.app.state.deps
        caller = await d.authenticate(match.group(1))
        if caller is not None:
            request.state.caller_id = caller.caller_id
        return caller

    async def guarded(handler: Awaitable[Response]) -> Response:
        """Storage/database failures -> 503 'uncertain, retry with the same key'.
        Only the exception type is logged; its text may contain SQL or values."""
        try:
            return await handler
        except Exception as exc:
            log.error("request failed error_type=%s", type(exc).__name__)
            return problem(
                503, "service_unavailable", [ErrorItem(code="outcome_uncertain_retry_same_key")]
            )

    def unauthenticated() -> JSONResponse:
        return problem(401, "unauthenticated", headers={"WWW-Authenticate": "Bearer"})

    async def submit(request: Request, channel: Channel) -> Response:
        d: ApiDependencies = request.app.state.deps
        caller = await caller_of(request)
        if caller is None:
            return unauthenticated()
        if channel not in caller.allowed_channels:
            return problem(403, "forbidden", [ErrorItem(code="channel_not_permitted")])
        if not _JSON_MEDIA.fullmatch(request.headers.get("content-type", "").strip()):
            return problem(415, "unsupported_media_type")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > d.max_body_bytes):
            return problem(413, "payload_too_large")
        # Read incrementally and stop as soon as the limit is exceeded: a missing or
        # false Content-Length (e.g. chunked) can never make us buffer an unbounded body.
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > d.max_body_bytes:
                return problem(413, "payload_too_large")
            chunks.append(chunk)
        raw = b"".join(chunks)

        key = request.headers.get("idempotency-key")
        if key is None:
            return problem(
                422,
                "validation_failed",
                [ErrorItem(field="header.Idempotency-Key", code="required")],
            )
        if not re.fullmatch(IDEMPOTENCY_KEY_PATTERN, key):
            return problem(
                422,
                "validation_failed",
                [ErrorItem(field="header.Idempotency-Key", code="invalid_format")],
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return problem(422, "validation_failed", [ErrorItem(code="invalid_json")])
        if not isinstance(data, dict):
            return problem(422, "validation_failed", [ErrorItem(code="invalid_json")])
        model = SmsSubmission if channel is Channel.SMS else TelegramSubmission
        try:
            submission: Submission = model.model_validate(data)
        except ValidationError as exc:
            return problem(422, "validation_failed", validation_items(exc))

        if submission.purpose not in caller.allowed_purposes or (
            submission.purpose.value == "broadcast" and not request.app.state.deps.broadcast_enabled
        ):
            return problem(
                403, "forbidden", [ErrorItem(field="purpose", code="purpose_not_permitted")]
            )
        if channel is Channel.TELEGRAM and caller.telegram_bot is None:
            return problem(403, "forbidden", [ErrorItem(code="telegram_bot_not_configured")])

        try:
            stored, decision = await d.repository.create_or_get(caller, key, submission, d.clock())
        except IdempotencyConflict:
            return problem(
                409,
                "idempotency_conflict",
                [ErrorItem(field="header.Idempotency-Key", code="payload_mismatch")],
            )
        except ExpiryRejected as exc:
            return problem(422, "validation_failed", [ErrorItem(field="expires_at", code=exc.code)])
        except BindingIntegrityError:
            log.error("idempotency binding integrity error caller=%s", caller.caller_id)
            return problem(
                503, "service_unavailable", [ErrorItem(code="outcome_uncertain_retry_same_key")]
            )
        status = 202 if decision is IdempotencyDecision.NEW else 200
        return JSONResponse(
            status_code=status,
            content=_submission_response(stored),
            headers={"Location": f"/api/v1/messages/{stored.id}"},
        )

    # ---------------------------------------------------------------- endpoints

    @app.post("/api/v1/messages/sms", status_code=202)
    async def submit_sms(request: Request) -> Response:
        return await guarded(submit(request, Channel.SMS))

    @app.post("/api/v1/messages/telegram", status_code=202)
    async def submit_telegram(request: Request) -> Response:
        return await guarded(submit(request, Channel.TELEGRAM))

    @app.get("/api/v1/messages/{message_id}")
    async def get_message(request: Request, message_id: str) -> Response:
        return await guarded(status_of(request, message_id))

    async def status_of(request: Request, message_id: str) -> Response:
        caller = await caller_of(request)
        if caller is None:
            return unauthenticated()
        try:
            mid = uuid.UUID(message_id)
        except ValueError:
            return problem(404, "not_found")
        d: ApiDependencies = request.app.state.deps
        stored = await d.repository.get_for_caller(caller.caller_id, mid)
        if stored is None:
            return problem(404, "not_found")
        view: MessageStatusView = to_status_view(stored)
        return JSONResponse(content=view.model_dump(mode="json", exclude_none=True))

    # ------------------------------------------------------------------ charts (statistics)
    # The dashboard service contract of the OAN dashboards (oan_messaging/stats/charts.py):
    # read-only, counts only, for statistics readers (callers created with --stats-reader).

    async def charts(request: Request, chart_ids: list[str]) -> Response | dict[str, Any]:
        """The chart data, or the problem response that refuses it."""
        from oan_messaging.stats.charts import ChartQueryError

        caller = await caller_of(request)
        if caller is None:
            return unauthenticated()
        if not caller.can_read_stats:
            return problem(403, "forbidden", [ErrorItem(code="stats_not_permitted")])
        d: ApiDependencies = request.app.state.deps
        if d.stats is None:
            return problem(503, "service_unavailable")
        if not chart_ids:
            return problem(422, "validation_failed", [ErrorItem(field="charts", code="required")])
        try:
            result: dict[str, Any] = await d.stats.charts(chart_ids, dict(request.query_params))
        except KeyError:
            return problem(404, "not_found")
        except ChartQueryError as exc:
            return problem(422, "validation_failed", [ErrorItem(field=exc.field, code=exc.code)])
        return result

    @app.get("/api/v1/charts/{chart_id}")
    async def get_chart(request: Request, chart_id: str) -> Response:
        async def one() -> Response:
            result = await charts(request, [chart_id])
            if isinstance(result, Response):
                return result
            meta = {**result["meta"], "chart": chart_id}
            return JSONResponse({"success": True, "data": result["data"][chart_id], "meta": meta})

        return await guarded(one())

    @app.get("/api/v1/charts")
    async def get_charts(request: Request) -> Response:
        async def many() -> Response:
            raw = request.query_params.get("charts", "")
            ids = list(dict.fromkeys(c.strip() for c in raw.split(",") if c.strip()))[:20]
            result = await charts(request, ids)
            if isinstance(result, Response):
                return result
            data = {cid: {"chartName": cid, "data": rows} for cid, rows in result["data"].items()}
            return JSONResponse({"success": True, "data": data, "meta": result["meta"]})

        return await guarded(many())

    return app
