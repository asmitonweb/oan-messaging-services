"""Process-wide stdlib logging boundary for configured secrets.

Installed once, after Logger.makeRecord merges structured extras and before any
handler sees the record. Covers later loggers/handlers and exception formatting.
Direct writes to streams or handlers and third-party exporters bypassing stdlib
are outside this boundary: runtime must not enable them or capture locals.
"""

from __future__ import annotations

import logging
import re
import threading
import traceback
from typing import Any
from urllib.parse import quote, quote_plus

_LOCK = threading.RLock()
_SECRETS: set[str] = set()
_INSTALLED = False
_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:bot)?[0-9]{5,16}:[A-Za-z0-9_-]{30,64}")
_SENSITIVE = re.compile(
    r"token|secret|password|authorization|recipient|phone|chat_id|otp|body|text", re.I
)


def clean(value: str) -> str:
    with _LOCK:
        secrets = tuple(_SECRETS)
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    return _TOKEN.sub("bot<redacted>", value)


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<redacted>"
    if isinstance(value, str):
        return clean(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            clean(str(k)): "<redacted>" if _SENSITIVE.search(str(k)) else _safe(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth + 1) for v in value]
    # Do not hand arbitrary objects (with mutable/secret-bearing repr) to exporters.
    return "<redacted>"


def install_redaction(*secrets: str) -> None:
    global _INSTALLED
    with _LOCK:
        for secret in secrets:
            if secret:
                _SECRETS.update((secret, quote(secret, safe=""), quote_plus(secret)))
        if _INSTALLED:
            return
        original = logging.Logger.makeRecord

        def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
            record = original(self, *args, **kwargs)
            if record.name == "uvicorn.access" and isinstance(record.args, tuple):
                # uvicorn's AccessFormatter reads the structured args (client, method,
                # path, version, status) directly: keep them, each one redacted.
                record.args = tuple(clean(a) if isinstance(a, str) else a for a in record.args)
                record.msg = clean(str(record.msg))
            else:
                try:
                    record.msg = clean(record.getMessage())
                except Exception:
                    record.msg = "<unformattable log record>"
                record.args = ()
            if record.exc_info:
                # Render without locals, then drop the traceback object before export.
                record.exc_text = clean("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            for key, value in list(record.__dict__.items()):
                if key in ("msg", "args", "exc_info"):
                    continue
                record.__dict__[key] = (
                    "<redacted>" if _SENSITIVE.search(key) and key != "exc_text" else _safe(value)
                )
            return record

        logging.Logger.makeRecord = make_record  # type: ignore[method-assign]
        _INSTALLED = True
