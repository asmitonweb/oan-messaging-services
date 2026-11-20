"""RFC 9457 problem responses (``Problem`` from the contract) and redacted validation
errors. Nothing here ever includes a submitted value, a pydantic ``input`` or ``msg``,
or an unknown field's name (which could itself carry data)."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import ValidationError

from oan_messaging.contracts import ErrorItem, Problem

PROBLEM_JSON = "application/problem+json"

_TITLES = {
    401: "Unauthenticated",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Idempotency conflict",
    413: "Payload too large",
    415: "Unsupported media type",
    422: "Validation failed",
    503: "Service unavailable",
}


def problem(
    status: int,
    code: str,
    errors: list[ErrorItem] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        title=_TITLES.get(status, "Error"),
        status=status,
        code=code,
        errors=tuple(errors or ()),
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_JSON,
        headers=headers,
    )


# value_error messages from contracts.py validators -> stable, value-free codes.
_VALUE_ERROR_CODES = (
    ("ethiopian mobile", "invalid_et_mobile"),
    ("single sms part", "too_long_for_single_sms"),
    ("sms part limit", "too_long_for_sms"),
    ("telegram message length", "too_long"),
    ("must not be blank", "blank"),
    ("control characters", "control_characters"),
    ("telegram chat identifier", "invalid_chat_id"),
    ("expires_at is required", "required_for_otp"),
)


def _code_for(err: dict[str, Any]) -> str:
    etype = str(err.get("type", "invalid"))
    if etype == "value_error":
        msg = str(err.get("msg", "")).lower()
        for needle, code in _VALUE_ERROR_CODES:
            if needle in msg:
                return code
        return "invalid"
    return {
        "missing": "required",
        "extra_forbidden": "unknown_field",
        "string_pattern_mismatch": "invalid_format",
        "timezone_aware": "timezone_required",
    }.get(etype, etype)


def validation_items(exc: ValidationError) -> list[ErrorItem]:
    items: list[ErrorItem] = []
    for err in exc.errors(include_input=False, include_url=False):
        code = _code_for(dict(err))
        loc = [str(p) for p in err.get("loc", ())]
        if code == "unknown_field":
            field = None  # the unknown key name is caller data; do not echo it
        elif code == "required_for_otp":
            field = "expires_at"
        else:
            field = ".".join(loc) or None
        item = ErrorItem(field=field, code=code)
        if item not in items:
            items.append(item)
    return items
