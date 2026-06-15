from __future__ import annotations

import json
import logging

import pytest

from qbsvc.logging import JsonFormatter, configure_logging, reset_logging_for_tests
from qbsvc.request_context import reset_request_id, set_request_id


@pytest.fixture(autouse=True)
def _reset_logging():
    """Each test gets a clean root logger so handlers from earlier configure_logging
    calls don't double-emit lines.
    """
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    reset_logging_for_tests()
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def _format(record: logging.LogRecord) -> dict:
    formatter = JsonFormatter()
    return json.loads(formatter.format(record))


def _make_record(**kwargs) -> logging.LogRecord:
    return logging.LogRecord(
        name=kwargs.get("name", "qbsvc.test"),
        level=kwargs.get("level", logging.INFO),
        pathname=__file__,
        lineno=1,
        msg=kwargs.get("msg", "hello"),
        args=None,
        exc_info=None,
    )


def test_json_formatter_emits_standard_fields():
    rec = _make_record()
    payload = _format(rec)
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"
    assert payload["logger"] == "qbsvc.test"
    assert "timestamp" in payload
    # Timestamp must be a string the operator can read (ISO 8601, UTC).
    assert payload["timestamp"].endswith("Z") or "+" in payload["timestamp"]


def test_json_formatter_includes_request_id_when_set():
    token = set_request_id("req-abc-123")
    try:
        rec = _make_record()
        payload = _format(rec)
        assert payload["request_id"] == "req-abc-123"
    finally:
        reset_request_id(token)


def test_json_formatter_omits_request_id_when_unset():
    rec = _make_record()
    payload = _format(rec)
    assert "request_id" not in payload


def test_json_formatter_propagates_extras():
    rec = _make_record()
    rec.__dict__["method"] = "GET"
    rec.__dict__["status"] = 200
    rec.__dict__["duration_ms"] = 12.4
    payload = _format(rec)
    assert payload["method"] == "GET"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.4


def test_json_formatter_serializes_exception_info():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord(
            name="qbsvc.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=None,
            exc_info=True,
        )
        import sys

        rec.exc_info = sys.exc_info()
    payload = _format(rec)
    assert "exception" in payload
    assert "ValueError" in payload["exception"]
    assert "boom" in payload["exception"]


def test_configure_logging_installs_json_handler_once(capsys):
    configure_logging(level="INFO")
    configure_logging(level="INFO")  # second call must not double-install
    logger = logging.getLogger("qbsvc.test")
    logger.info("smoke")
    captured = capsys.readouterr()
    # Exactly one JSON-parseable line, not two.
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "smoke"


def test_configure_logging_writes_to_stdout(capsys):
    """Cloud Run reads stdout (not stderr) for structured logs by default."""
    configure_logging(level="INFO")
    logging.getLogger("qbsvc.smoke").info("hi")
    captured = capsys.readouterr()
    assert captured.out.strip() != ""
    assert captured.err.strip() == ""


def test_json_formatter_handles_non_serializable_extras():
    """An extra that can't be JSON-encoded must not blow up the log line."""

    class Weird:
        def __repr__(self):
            return "<Weird>"

    rec = _make_record()
    rec.__dict__["custom"] = Weird()
    payload = _format(rec)
    # Falls back to a string repr rather than raising.
    assert "Weird" in payload["custom"]
