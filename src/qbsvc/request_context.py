from __future__ import annotations

from contextvars import ContextVar

# Set by RequestIDMiddleware at the top of each request; read by the JSON
# logger, the QBO client, and the error envelope builder so every log line
# and error body carries the same correlation id.
#
# `None` outside of a request — log emission must not crash before the
# middleware has run (e.g. during app startup or in a non-HTTP context like a
# CLI).
_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return _request_id_var.get()


def set_request_id(value: str | None) -> object:
    """Set the current request id and return the token for later reset."""
    return _request_id_var.set(value)


def reset_request_id(token: object) -> None:
    _request_id_var.reset(token)  # type: ignore[arg-type]
