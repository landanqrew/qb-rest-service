from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from qbsvc.request_context import reset_request_id, set_request_id

REQUEST_ID_HEADER = "X-Request-ID"

# Trust caller-supplied ids only if they look like sane correlation tokens.
# Permissive enough for UUIDs, Cloud Trace ids, ULIDs, and short slugs;
# tight enough that a 1KB blob or a control-character payload can't poison
# log lines or response headers.
_ALLOWED_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")

_access_log = logging.getLogger("qbsvc.access")


class RequestIDMiddleware:
    """Bind a request id to the request scope and emit one access log per request.

    Pure-ASGI on purpose: starlette's BaseHTTPMiddleware runs the inner app in
    a separate task, which copies — rather than shares — the ContextVar
    context. That means a ContextVar set in the outer task is visible to the
    inner task on the way in, but later writes (e.g. exception handlers
    setting fields) and the timing of resets get out of sync, and a bare
    `get_request_id()` from inside an exception handler can come back None.
    Implementing the middleware at the ASGI layer keeps everything in one
    task and one context.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request_id = _resolve_request_id(_header(scope, REQUEST_ID_HEADER))

        # Stash on scope.state so route handlers and exception handlers can
        # read it via `request.state.request_id` without touching the
        # ContextVar machinery.
        scope.setdefault("state", {})["request_id"] = request_id

        token = set_request_id(request_id)
        start = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers") or [])
                # Echo the id back so callers can correlate. Strip any
                # accidental duplicate added downstream.
                headers = [
                    (k, v) for (k, v) in headers if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        method = scope.get("method", "")
        path = scope.get("path", "")
        response_started = {"value": False}

        async def started_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_started["value"] = True
            await send_wrapper(message)

        try:
            await self._app(scope, receive, started_wrapper)
        except Exception as exc:
            # An unhandled exception reached us. The default Starlette path is
            # ServerErrorMiddleware (outermost), which would respond with a
            # bare 500 via its own `send` — bypassing our wrapper and
            # stripping the X-Request-ID header from the response. Catch it
            # here so we can emit the envelope through `send_wrapper` and
            # preserve the correlation id.
            _access_log.exception(
                "unhandled exception in request",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "exc_type": type(exc).__name__,
                },
            )
            if not response_started["value"]:
                await _send_internal_error(send_wrapper, request_id)
                status_holder["status"] = 500
            else:
                # Headers already flushed; we cannot rewrite the response.
                # Re-raise so ServerErrorMiddleware does its usual cleanup.
                raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            _access_log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status": status_holder["status"],
                    "duration_ms": duration_ms,
                },
            )
            reset_request_id(token)


async def _send_internal_error(send: Send, request_id: str) -> None:
    """Emit a 500 envelope using the same shape as `qbsvc.errors.envelope`.

    Inlined to avoid an import cycle (errors → schemas → request_context;
    middleware sits next to all three).
    """
    body = json.dumps(
        {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Internal server error",
                "request_id": request_id,
            }
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _header(scope: Scope, name: str) -> str | None:
    target = name.lower().encode("ascii")
    for k, v in scope.get("headers", []):
        if k.lower() == target:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


def _resolve_request_id(supplied: str | None) -> str:
    if supplied and _ALLOWED_ID.match(supplied):
        return supplied
    return uuid.uuid4().hex
