from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone

from qbsvc.request_context import get_request_id

# Fields that the stdlib LogRecord populates and the JSON formatter promotes
# to top-level keys. Everything else attached via `extra={}` falls through
# verbatim (filtered against this set so internal record bookkeeping doesn't
# pollute the log line).
_STANDARD_RECORD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Single-line JSON log records for Cloud Run / GCP Logging.

    Always emits `timestamp`, `level`, `logger`, `message`. Adds `request_id`
    when the request-scoped ContextVar is set. Any `extra={...}` keys
    passed to the logger flow through as top-level fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": _iso_utc(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = get_request_id()
        if request_id is not None:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=_json_default, separators=(",", ":"))


def _iso_utc(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _json_default(value: object) -> str:
    return repr(value)


_CONFIGURED = False
_CONFIGURE_LOCK = threading.Lock()


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger, writing to stdout.

    Idempotent and thread-safe — concurrent first-time callers (e.g. multiple
    worker threads racing to invoke create_app()) cannot both pass the
    `_CONFIGURED` check, so handlers never stack and log lines never
    duplicate.
    """
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(level)

    with _CONFIGURE_LOCK:
        if _CONFIGURED:
            return
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler.set_name("qbsvc.json")
        root.addHandler(handler)
        _CONFIGURED = True


def reset_logging_for_tests() -> None:
    """Test hook — drops the configured handler so the next configure_logging
    starts from scratch. Never call from production code."""
    global _CONFIGURED
    _CONFIGURED = False
    root = logging.getLogger()
    for h in [h for h in root.handlers if h.get_name() == "qbsvc.json"]:
        root.removeHandler(h)
