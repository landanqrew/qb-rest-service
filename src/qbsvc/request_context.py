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


# Intuit returns a transaction id (`intuit_tid` header) on every QBO response.
# The QBO client binds it here so log lines and the error envelope can echo it
# back — Intuit support uses this value to trace a request on their side.
#
# `None` outside of (or before) a QBO call. Bound per QBClient request; in the
# sync route path the value is set on the threadpool worker's copied context,
# so an error envelope built later in the same handler sees it.
_intuit_tid_var: ContextVar[str | None] = ContextVar("intuit_tid", default=None)


def get_intuit_tid() -> str | None:
    return _intuit_tid_var.get()


def set_intuit_tid(value: str | None) -> None:
    _intuit_tid_var.set(value)
