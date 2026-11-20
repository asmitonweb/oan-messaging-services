"""MSG-06 correction 3: untrusted error bodies never reach exception text, reprs,
attributes or logs. Only contract-known, value-free codes and field names survive; any
other value becomes ``unrecognized``. Malformed shapes never raise raw exceptions.
MOCKED HTTP only; fake credentials and 0900000xxx numbers."""

from __future__ import annotations

import ast
import json
import logging
import re
import typing
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FAKE_TOKEN, ROOT, FakeClock, client_for
from oan_client import OanConflict, OanRejected
from oan_client.client import ERROR_CODES, ERROR_FIELDS, PROBLEM_CODES, UNPARSEABLE, UNRECOGNIZED
from test_client import KEY, MID, OTP_TEXT, otp, sequence

from oan_messaging.api import problems as api_problems
from oan_messaging.contracts import Problem

OTP = "482913"
PHONE = "0900000001"
SECRETS = (OTP_TEXT, OTP, PHONE, "+251900000001", FAKE_TOKEN, FAKE_TOKEN[7:], KEY,
           "fake-secret-leak", "private-phone", "private-body")


def echo_body(status: int) -> dict[str, Any]:
    """A hostile/misbehaving server echoing request data into every error field."""
    return {
        "type": f"https://evil.invalid/{OTP}", "title": OTP_TEXT, "status": status,
        "detail": f"recipient {PHONE} token {FAKE_TOKEN}", "code": "fake-secret-leak",
        "errors": [
            {"field": "private-phone", "code": "private-body"},
            {"field": PHONE, "code": OTP},
            {"field": "text", "code": OTP_TEXT},
            {"field": f"header.Authorization {FAKE_TOKEN}", "code": "invalid"},
            {"field": "recipient", "code": "invalid_et_mobile", "value": PHONE},
        ],
    }


def everything_printable(exc: BaseException, caplog: pytest.LogCaptureFixture) -> str:
    return " ".join([str(exc), repr(exc), repr(exc.args), repr(vars(exc)), caplog.text,
                     " ".join(str(r.args) for r in caplog.records),
                     logging.Formatter().formatException((type(exc), exc, None))])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 415, 422])
async def test_echoed_values_never_appear_in_exceptions_or_logs(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, status: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    c, _ = client_for(sequence(httpx.Response(status, json=echo_body(status))), clock)
    with pytest.raises(OanRejected) as info:
        await c.submit(otp())
    shown = everything_printable(info.value, caplog)
    for needle in SECRETS:
        assert needle not in shown, needle
    exc = info.value
    assert exc.code == UNRECOGNIZED and exc.status == status
    assert exc.errors == ((None, UNRECOGNIZED), ("text", UNRECOGNIZED), (None, "invalid"),
                          ("recipient", "invalid_et_mobile"))
    assert isinstance(exc, OanConflict) == (status == 409)
    log = logging.getLogger("t")
    log.error("dispatch failed: %s", exc, exc_info=exc)  # what a caller might log
    assert all(n not in caplog.text for n in SECRETS)
    await c.aclose()


async def test_status_lookup_errors_are_redacted_too(
    clock: FakeClock, caplog: pytest.LogCaptureFixture,
) -> None:
    c, _ = client_for(sequence(httpx.Response(404, json=echo_body(404))), clock)
    with pytest.raises(OanRejected) as info:
        await c.get_status(MID)
    shown = everything_printable(info.value, caplog)
    assert all(n not in shown for n in SECRETS)
    await c.aclose()


MALFORMED: list[tuple[str, Any]] = [
    ("errors-null", {"code": "validation_failed", "errors": None}),
    ("errors-string", {"code": "validation_failed", "errors": OTP_TEXT}),
    ("errors-object", {"code": "validation_failed", "errors": {PHONE: OTP}}),
    ("errors-scalars", {"code": "validation_failed", "errors": [1, OTP, None, True]}),
    ("errors-nested", {"code": "validation_failed",
                       "errors": [{"field": [PHONE], "code": {"otp": OTP}}]}),
    ("code-number", {"code": 422, "errors": []}),
    ("code-object", {"code": {"leak": OTP}}),
    ("code-list", {"code": [OTP_TEXT]}),
    ("top-level-list", [OTP, PHONE]),
    ("top-level-string", OTP_TEXT),
    ("top-level-null", None),
]


@pytest.mark.parametrize(("name", "body"), MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_malformed_error_shapes_are_handled_without_raw_exceptions(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, name: str, body: Any,
) -> None:
    c, _ = client_for(sequence(httpx.Response(422, json=body)), clock)
    with pytest.raises(OanRejected) as info:
        await c.submit(otp())
    assert info.value.code in (UNRECOGNIZED, UNPARSEABLE, "validation_failed")
    for _field, code in info.value.errors:
        assert code == UNRECOGNIZED and _field is None
    assert all(n not in everything_printable(info.value, caplog) for n in SECRETS)
    await c.aclose()


@pytest.mark.parametrize("content", [
    b"not json " + OTP_TEXT.encode(), b"\xff\xfe\x00garbage", b"",
    b"[" * 8000 + b"]" * 8000,  # deep nesting under the size cap (RecursionError path)
    b"[" * 100_000 + b"]" * 100_000,  # deep nesting over the size cap
    json.dumps({"code": "validation_failed", "detail": OTP * 5000}).encode(),  # oversized
])
async def test_unparseable_bodies_become_a_fixed_protocol_code(
    clock: FakeClock, caplog: pytest.LogCaptureFixture, content: bytes,
) -> None:
    c, _ = client_for(sequence(httpx.Response(422, content=content,
                                              headers={"content-type": "application/json"})),
                      clock)
    with pytest.raises(OanRejected) as info:
        await c.submit(otp())
    assert (info.value.code, info.value.errors) == (UNPARSEABLE, ())
    assert all(n not in everything_printable(info.value, caplog) for n in SECRETS)
    await c.aclose()


async def test_error_items_are_bounded_and_deduplicated(clock: FakeClock) -> None:
    errors = [{"field": "text", "code": "blank"}] * 50 + [
        {"field": "recipient", "code": f"x{i}"} for i in range(50)]
    c, _ = client_for(sequence(httpx.Response(422, json={"code": "validation_failed",
                                                         "errors": errors})), clock)
    with pytest.raises(OanRejected) as info:
        await c.submit(otp())
    assert info.value.errors == (("text", "blank"),)  # first 16 items, deduplicated
    await c.aclose()


async def test_known_contract_values_are_kept(clock: FakeClock) -> None:
    c, _ = client_for(sequence(httpx.Response(422, json={
        "title": "Validation failed", "status": 422, "code": "validation_failed",
        "errors": [{"field": "text", "code": "too_long_for_single_sms"},
                   {"field": "header.Idempotency-Key", "code": "invalid_format"},
                   {"code": "invalid_json"}]})), clock)
    with pytest.raises(OanRejected) as info:
        await c.submit(otp())
    assert info.value.code == "validation_failed"
    assert info.value.errors == (("text", "too_long_for_single_sms"),
                                 ("header.Idempotency-Key", "invalid_format"),
                                 (None, "invalid_json"))
    await c.aclose()


# ------------------------------------- allow-lists cover what the accepted API emits


def test_problem_codes_equal_the_frozen_contract() -> None:
    annotation = Problem.model_fields["code"].annotation
    assert set(typing.get_args(annotation)) == PROBLEM_CODES


def _literal_codes_in(path: Path) -> set[str]:
    codes: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.keyword) and node.arg == "code" and isinstance(
                node.value, ast.Constant) and isinstance(node.value.value, str):
            codes.add(node.value.value)
    return codes


def test_every_error_code_the_api_emits_is_allow_listed() -> None:
    emitted = _literal_codes_in(ROOT / "oan_messaging" / "api" / "app.py")
    emitted |= {code for _needle, code in api_problems._VALUE_ERROR_CODES}  # noqa: SLF001
    tree = ast.parse((ROOT / "oan_messaging" / "api" / "problems.py").read_text())
    code_for = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_code_for")
    for node in ast.walk(code_for):  # the pydantic-type -> code mapping and its default
        if isinstance(node, ast.Dict):
            emitted |= {v.value for v in node.values
                        if isinstance(v, ast.Constant) and isinstance(v.value, str)}
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
            emitted.add(str(node.value.value))
    assert {"required", "unknown_field", "invalid_format", "timezone_required",
            "invalid"} <= emitted
    repo = (ROOT / "oan_messaging" / "storage" / "repository.py").read_text()
    expiry = set(re.findall(r'return "([a-z_]+)"', repo[repo.index("def _expiry_code"):
                                                         repo.index("def _fp(")]))
    assert expiry == {"required_for_otp", "not_in_future", "beyond_ceiling"}
    emitted |= expiry
    assert emitted <= ERROR_CODES, emitted - ERROR_CODES


def test_allow_lists_contain_only_value_free_identifiers() -> None:
    for value in ERROR_CODES | PROBLEM_CODES:
        assert re.fullmatch(r"[a-z][a-z0-9_]{1,40}", value), value
    for value in ERROR_FIELDS:
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{1,40}", value), value
