from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from qbsvc.middleware import RequestIDMiddleware
from qbsvc.request_context import get_request_id


UUID_HEX = re.compile(r"^[0-9a-fA-F-]{8,}$")


@pytest.fixture
def app_with_middleware() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/echo")
    def echo() -> dict:
        return {"request_id": get_request_id()}

    return app


def test_middleware_generates_request_id_when_header_absent(app_with_middleware):
    client = TestClient(app_with_middleware)
    resp = client.get("/echo")
    assert resp.status_code == 200
    header_id = resp.headers["X-Request-ID"]
    assert UUID_HEX.match(header_id)
    # The same id must be visible inside the handler via the ContextVar.
    assert resp.json()["request_id"] == header_id


def test_middleware_propagates_caller_supplied_request_id(app_with_middleware):
    """Acceptance: caller-supplied X-Request-ID is propagated, not overwritten."""
    client = TestClient(app_with_middleware)
    resp = client.get("/echo", headers={"X-Request-ID": "caller-abc-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "caller-abc-123"
    assert resp.json()["request_id"] == "caller-abc-123"


def test_middleware_rejects_unreasonable_request_id_and_generates_fresh(
    app_with_middleware,
):
    """An empty or absurdly long caller-supplied header is ignored so a hostile
    or buggy client cannot poison log correlation."""
    client = TestClient(app_with_middleware)
    resp = client.get("/echo", headers={"X-Request-ID": ""})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] != ""
    assert UUID_HEX.match(resp.headers["X-Request-ID"])

    too_long = "x" * 1024
    resp2 = client.get("/echo", headers={"X-Request-ID": too_long})
    assert resp2.status_code == 200
    assert resp2.headers["X-Request-ID"] != too_long


def test_request_id_isolated_across_requests(app_with_middleware):
    """Two sequential requests must not leak request ids into each other.

    Guards against accidentally setting the ContextVar at module scope or
    forgetting to reset it after the response completes.
    """
    client = TestClient(app_with_middleware)
    r1 = client.get("/echo")
    r2 = client.get("/echo")
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


def test_get_request_id_outside_request_returns_none():
    """Before any middleware runs, request_id must be None — not a stale value."""
    assert get_request_id() is None
