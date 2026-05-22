"""End-to-end coverage of the four issue #10 acceptance criteria.

Lower-level unit tests cover the components in isolation; this module wires
the actual FastAPI app via `create_app()` so a regression that breaks the
middleware/handler hookup gets caught here even if every unit test still
passes.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import get_settings
from qbsvc.deps import (
    get_qb_client,
    get_token_store,
    reset_token_store_cache,
)
from qbsvc.logging import JsonFormatter, reset_logging_for_tests
from qbsvc.main import create_app


@pytest.fixture(autouse=True)
def _reset():
    get_settings.cache_clear()
    reset_token_store_cache()
    reset_logging_for_tests()
    yield
    get_settings.cache_clear()
    reset_token_store_cache()
    reset_logging_for_tests()


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_SECRET", "test-secret")


@pytest.fixture
def token_store(tmp_path: Path) -> FileTokenStore:
    store = FileTokenStore(path=tmp_path / "tokens.json")
    store.save(
        TokenData(
            access_token="acc",
            refresh_token="ref",
            realm_id="realm-INT",
            expires_at=time.time() + 3600,
        )
    )
    return store


def _app_with_qbo(token_store, handler) -> TestClient:
    transport = httpx.MockTransport(handler)
    app = create_app()

    def fake_dep():
        c = QBClient(token_store=token_store, settings=get_settings())
        c._http.close()
        c._http = httpx.Client(transport=transport, timeout=30)
        try:
            yield c
        finally:
            c.close()

    app.dependency_overrides[get_qb_client] = fake_dep
    app.dependency_overrides[get_token_store] = lambda: token_store
    return TestClient(app)


# ---------- AC1: every response includes X-Request-ID ----------


def test_healthz_response_includes_request_id_header():
    app = create_app()
    c = TestClient(app)
    resp = c.get("/healthz")
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers
    assert resp.headers["X-Request-ID"] != ""


def test_v1_customers_response_includes_request_id_header(settings_env, token_store):
    def handler(req):
        return httpx.Response(
            200, json={"QueryResponse": {"Customer": []}}
        )

    c = _app_with_qbo(token_store, handler)
    resp = c.get("/v1/customers")
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers


# ---------- AC4: caller-supplied X-Request-ID is propagated ----------


def test_caller_supplied_request_id_is_propagated_across_app(settings_env, token_store):
    def handler(req):
        return httpx.Response(200, json={"QueryResponse": {"Customer": []}})

    c = _app_with_qbo(token_store, handler)
    resp = c.get(
        "/v1/customers",
        headers={"X-Request-ID": "from-web-app"},
    )
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "from-web-app"


# ---------- AC3: error envelope on every non-2xx response ----------


def test_unauthenticated_error_has_envelope_shape(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "empty.json")

    def handler(req):
        pytest.fail("QBO should not be hit when not authenticated")

    c = _app_with_qbo(empty_store, handler)
    resp = c.get("/v1/customers")
    assert resp.status_code == 503
    body = resp.json()
    # Envelope shape: top-level "error" with code, message, request_id.
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "NOT_AUTHENTICATED"
    assert "message" in body["error"]
    assert body["error"]["request_id"] == resp.headers["X-Request-ID"]


def test_validation_error_uses_envelope_shape(settings_env, token_store):
    def handler(req):
        pytest.fail("limit validation rejects before QBO hit")

    c = _app_with_qbo(token_store, handler)
    # limit must be >=1 — passing 0 trips pydantic validation.
    resp = c.get("/v1/customers?limit=0")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["request_id"] == resp.headers["X-Request-ID"]


def test_uncaught_exception_returns_envelope_with_request_id(settings_env, token_store):
    """Even a synthetic 500 must come out as the envelope with the request id."""

    def handler(req):
        return httpx.Response(200, json={})

    c = _app_with_qbo(token_store, handler)
    # Force an uncaught error by overriding a dependency to raise.
    from qbsvc.main import create_app as _ca

    app = _ca()

    def boom():
        raise RuntimeError("synthetic")

    app.dependency_overrides[get_qb_client] = boom
    cli = TestClient(app, raise_server_exceptions=False)
    resp = cli.get("/v1/customers", headers={"X-Request-ID": "trace-uncaught"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # The message must NOT leak the underlying exception text to the client.
    assert "synthetic" not in body["error"]["message"]
    assert body["error"]["request_id"] == "trace-uncaught"
    assert resp.headers["X-Request-ID"] == "trace-uncaught"


# ---------- AC2: every QBO call produces a structured log line ----------


def test_qbo_call_produces_structured_log_line_with_request_id(
    settings_env, token_store, caplog
):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req):
        return httpx.Response(200, json={"QueryResponse": {"Customer": []}})

    c = _app_with_qbo(token_store, handler)
    resp = c.get("/v1/customers", headers={"X-Request-ID": "trace-qbo"})
    assert resp.status_code == 200

    qbo = [r for r in caplog.records if r.name == "qbsvc.qbo"]
    assert qbo, "expected at least one qbsvc.qbo log record"
    rec = qbo[-1]
    # Format via the prod JsonFormatter, re-entering the request context so
    # request_id binding mirrors the live path.
    from qbsvc.request_context import reset_request_id, set_request_id

    token = set_request_id("trace-qbo")
    try:
        payload = json.loads(JsonFormatter().format(rec))
    finally:
        reset_request_id(token)

    # All scope §10 fields land in the structured record.
    assert payload["level"] == "INFO"
    assert payload["method"] == "GET"
    assert payload["qbo_endpoint"] == "query"
    assert payload["status"] == 200
    assert payload["realm_id"] == "realm-INT"
    assert payload["request_id"] == "trace-qbo"
    assert isinstance(payload["qbo_duration_ms"], (int, float))


def test_access_log_emits_request_record_per_http_call(settings_env, token_store, caplog):
    caplog.set_level(logging.INFO, logger="qbsvc.access")

    def handler(req):
        return httpx.Response(200, json={"QueryResponse": {"Customer": []}})

    c = _app_with_qbo(token_store, handler)
    c.get("/healthz")
    c.get("/v1/customers", headers={"X-Request-ID": "trace-access"})

    access = [r for r in caplog.records if r.name == "qbsvc.access"]
    # /healthz + /v1/customers → at least two access records.
    paths = [r.__dict__.get("path") for r in access]
    assert "/healthz" in paths
    assert "/v1/customers" in paths

    # The /v1/customers record must carry the caller's request id.
    cust_rec = next(
        r for r in access if r.__dict__.get("path") == "/v1/customers"
    )
    assert cust_rec.__dict__["request_id"] == "trace-access"
    assert cust_rec.__dict__["status"] == 200
    assert cust_rec.__dict__["method"] == "GET"
    assert isinstance(cust_rec.__dict__["duration_ms"], (int, float))
