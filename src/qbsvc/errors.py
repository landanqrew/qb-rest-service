from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from qbsvc.exceptions import (
    APIError,
    AuthError,
    RateLimitError,
    TokenStoreError,
)
from qbsvc.request_context import get_intuit_tid, get_request_id
from qbsvc.schemas import ErrorPayload, ErrorResponse

_log = logging.getLogger("qbsvc.errors")


def envelope(
    code: str,
    message: str,
    status: int,
    *,
    qbo_detail: str | None = None,
    intuit_tid: str | None = None,
) -> JSONResponse:
    """Build the standard error envelope, attaching the active request id.

    `intuit_tid` defaults to the value the QBO client bound to the request
    context, so inline route error responses pick it up automatically. The
    central APIError handler passes it explicitly because it runs in the async
    context, outside the sync worker where the client bound it.
    """
    payload = ErrorResponse(
        error=ErrorPayload(
            code=code,
            message=message,
            request_id=get_request_id(),
            qbo_detail=qbo_detail,
            intuit_tid=intuit_tid or get_intuit_tid(),
        )
    )
    return JSONResponse(
        status_code=status,
        content=payload.model_dump(exclude_none=True),
    )


def _is_qbo_not_found(exc: APIError) -> bool:
    # QBO surfaces missing entities as HTTP 400 with Fault.Error code 610
    # ("Object Not Found"). Honor that here so an uncaught APIError still
    # turns into a clean 404 rather than a 502.
    if exc.status_code == 404:
        return True
    if exc.status_code == 400 and "object not found" in exc.detail.lower():
        return True
    return False


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers that map every exception to the error envelope.

    Routes can still catch domain exceptions inline for finer-grained
    branching (e.g. attaching a label to NOT_FOUND messages); these handlers
    catch whatever bubbles past them so a 500 with a bare traceback never
    reaches the caller.
    """

    @app.exception_handler(AuthError)
    async def _auth(_request: Request, exc: AuthError) -> JSONResponse:
        # `code` is set by every AuthError subclass so callers get a stable
        # identifier (NOT_AUTHENTICATED, TOKEN_REFRESH_FAILED, …) regardless
        # of how the message is worded.
        return envelope(exc.code, str(exc), status=503)

    @app.exception_handler(TokenStoreError)
    async def _store(_request: Request, exc: TokenStoreError) -> JSONResponse:
        return envelope("TOKEN_STORE_FAILED", str(exc), status=503)

    @app.exception_handler(RateLimitError)
    async def _rate(_request: Request, exc: RateLimitError) -> JSONResponse:
        response = envelope("RATE_LIMITED", str(exc), status=429)
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(APIError)
    async def _api(_request: Request, exc: APIError) -> JSONResponse:
        if _is_qbo_not_found(exc):
            return envelope(
                "NOT_FOUND",
                exc.detail or "Not found",
                status=404,
                qbo_detail=exc.detail,
                intuit_tid=exc.intuit_tid,
            )
        return envelope(
            "QBO_ERROR",
            str(exc),
            status=502,
            qbo_detail=exc.detail,
            intuit_tid=exc.intuit_tid,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Reshape FastAPI's pydantic-flavored 422 body into our envelope.
        # All field failures are joined into one message so the caller can
        # fix every bad field in a single round-trip.
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []))
            msg = err.get("msg", "Invalid request")
            parts.append(f"{loc}: {msg}" if loc else msg)
        message = "; ".join(parts) if parts else "Invalid request"
        return envelope("VALIDATION_ERROR", message, status=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return envelope(
            f"HTTP_{exc.status_code}",
            str(exc.detail) if exc.detail else "",
            status=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        # Log the full exception server-side; return a generic message to the
        # client to avoid leaking stack traces or internal details over the
        # wire (and to satisfy the envelope shape requirement).
        _log.exception("unhandled exception", extra={"exc_type": type(exc).__name__})
        return envelope("INTERNAL_ERROR", "Internal server error", status=500)
