"""Structured logging with request/job correlation and secret redaction.

Every record can answer the question the assignment asks (section 24, section 66):
*what happened to game X during crawl run Y*. That is why the correlation fields are
carried in contextvars rather than passed by hand at every call site.

No external logging library: stdlib ``logging`` plus ``python-json-logger`` is enough,
and one less dependency is one less thing to keep current.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.config import SECRET_FIELD_MARKERS, Settings

# --------------------------------------------------------------------------- context

# An immutable default: a shared mutable dict would leak correlation between requests.
_EMPTY: Mapping[str, Any] = MappingProxyType({})
_CORRELATION: ContextVar[Mapping[str, Any]] = ContextVar("correlation", default=_EMPTY)

CORRELATION_KEYS = (
    "request_id",
    "crawl_run_id",
    "job_id",
    "job_type",
    "game_id",
    "slug",
    "stage",
    "worker_id",
)


def get_correlation() -> dict[str, Any]:
    return dict(_CORRELATION.get())


@contextmanager
def correlate(**fields: Any) -> Iterator[None]:
    """Attach correlation fields to every log record emitted inside the block."""
    current = dict(_CORRELATION.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    token = _CORRELATION.set(current)
    try:
        yield
    finally:
        _CORRELATION.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------- redaction

_REDACTED = "***"


_SECRET_WORDS = frozenset(
    {"key", "token", "password", "passwd", "secret", "authorization", "auth", "cookie", "apikey"}
)


def _is_secret_key(key: str) -> bool:
    """True for ``llm_api_key``, ``ADMIN_TOKEN`` and also a bare ``token``.

    Matching only on ``_key``-style suffixes missed bare names, which is exactly the
    shape a third-party payload arrives in.
    """
    low = key.lower()
    if any(marker in low for marker in SECRET_FIELD_MARKERS):
        return True
    parts = re.split(r"[^a-z0-9]+", low)
    return any(part in _SECRET_WORDS for part in parts)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively blank out values whose key looks like a secret.

    Applied to log payloads AND to error messages that reach ``job_events`` and the
    monitoring UI, so an operator never reads a token off a dashboard.
    """
    if _depth > 6:
        return value
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_secret_key(str(k)) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v, _depth + 1) for v in value)
    return value


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in get_correlation().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class _RedactingJsonFormatter(JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        for key in CORRELATION_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                log_record[key] = value
        redacted = redact(log_record)
        log_record.clear()
        log_record.update(redacted)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: getattr(record, k) for k in CORRELATION_KEYS if getattr(record, k, None)}
        if extras:
            base += " | " + " ".join(f"{k}={v}" for k, v in redact(extras).items())
        return base


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())
    if settings.log_format == "json":
        handler.setFormatter(
            _RedactingJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    else:
        handler.setFormatter(_ConsoleFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # These are chatty and say nothing we do not already log ourselves.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
