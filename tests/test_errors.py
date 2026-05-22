from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from qbsvc.errors import register_exception_handlers
from qbsvc.exceptions import (
    APIError,
    AuthError,
    NotAuthenticatedError,
    RateLimitError,
    TokenRefreshError,
    TokenStoreError,
)
from qbsvc.middleware import RequestIDMiddleware


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/auth-error")
    def _auth():
        raise AuthError("token gone bad")

    @app.get("/not-authenticated")
    def _not_auth():
        raise NotAuthenticatedError("run the OAuth flow")

    @app.get("/refresh-failed")
    def _refresh_failed():
        raise TokenRefreshError("invalid_grant")

    @app.get("/store-error")
    def _store_err():
        raise TokenStoreError("Secret Manager unreachable")

    @app.get("/rate-limited")
    def _rate():
        raise RateLimitError("Slow down")

    @app.get("/qbo-error")
    def _api():
        raise APIError(400, "DocNumber 26-02-71 already exists")

    @app.get("/qbo-not-found")
    def _api_not_found():
        raise APIError(400, "Object Not Found: missing")

    @app.get("/validation/{n}")
    def _validation(n: int):
        return {"n": n}

    @app.get("/uncaught")
    def _uncaught():
        raise RuntimeError("kaboom")

    @app.get("/http-exc")
    def _http_exc():
        raise HTTPException(status_code=418, detail="i am a teapot")

    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------- shape ----------


def test_auth_error_returns_503_envelope(client):
    resp = client.get("/auth-error")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "AUTH_ERROR"
    assert "token gone bad" in body["error"]["message"]


def test_not_authenticated_uses_typed_code(client):
    """Subclass `code` attribute must override the base AuthError code."""
    resp = client.get("/not-authenticated")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_token_refresh_failed_uses_typed_code(client):
    resp = client.get("/refresh-failed")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "TOKEN_REFRESH_FAILED"


def test_token_store_error_returns_503_envelope(client):
    resp = client.get("/store-error")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "TOKEN_STORE_FAILED"
    assert "Secret Manager" in body["error"]["message"]


def test_rate_limit_returns_429_envelope(client):
    resp = client.get("/rate-limited")
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "RATE_LIMITED"


def test_api_error_returns_502_envelope_with_qbo_detail(client):
    resp = client.get("/qbo-error")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"
    assert body["error"]["qbo_detail"] == "DocNumber 26-02-71 already exists"


def test_api_error_object_not_found_returns_404(client):
    """Acceptance: NOT_FOUND mapping must be honored by the handler too —
    not just by callers that wrap APIError in their own try/except."""
    resp = client.get("/qbo-not-found")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"


def test_validation_error_returns_400_envelope(client):
    """FastAPI's default RequestValidationError 422 leaks pydantic detail
    shape that doesn't match our envelope. The handler reshapes it."""
    resp = client.get("/validation/not-a-number")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "message" in body["error"]


def test_validation_error_message_concatenates_all_fields():
    """When several fields fail at once the caller deserves to see all of
    them in one round-trip instead of fixing them one at a time.
    """
    from fastapi import Query

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    # Default-form annotations dodge the `from __future__ import annotations`
    # forward-ref evaluation pitfall that bites `Annotated[...]` inside a
    # closure-defined route.
    @app.get("/multi")
    def multi(
        alpha: int = Query(ge=1),
        beta: int = Query(ge=1),
    ) -> dict:
        return {"alpha": alpha, "beta": beta}

    c = TestClient(app, raise_server_exceptions=False)
    # Both query params violate ge=1 at the same time — handler must surface
    # both, not just the first.
    resp = c.get("/multi?alpha=0&beta=0")
    assert resp.status_code == 400
    message = resp.json()["error"]["message"]
    assert "alpha" in message
    assert "beta" in message
    # Multiple errors joined with a separator (semicolon, per handler impl).
    assert ";" in message


def test_uncaught_exception_returns_500_envelope(client):
    resp = client.get("/uncaught")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # Never leak internal exception text to clients — only the code.
    assert "kaboom" not in body["error"]["message"]


def test_http_exception_is_wrapped_in_envelope(client):
    resp = client.get("/http-exc")
    assert resp.status_code == 418
    body = resp.json()
    assert body["error"]["code"] == "HTTP_418"
    assert body["error"]["message"] == "i am a teapot"


# ---------- request_id propagation ----------


def test_error_envelope_includes_generated_request_id(client):
    resp = client.get("/auth-error")
    body = resp.json()
    rid = resp.headers["X-Request-ID"]
    assert body["error"]["request_id"] == rid


def test_error_envelope_uses_caller_supplied_request_id(client):
    resp = client.get(
        "/qbo-error", headers={"X-Request-ID": "trace-from-caller"}
    )
    assert resp.headers["X-Request-ID"] == "trace-from-caller"
    assert resp.json()["error"]["request_id"] == "trace-from-caller"


def test_uncaught_exception_still_carries_request_id(client):
    resp = client.get("/uncaught", headers={"X-Request-ID": "trace-1"})
    body = resp.json()
    assert body["error"]["request_id"] == "trace-1"
    assert resp.headers["X-Request-ID"] == "trace-1"
