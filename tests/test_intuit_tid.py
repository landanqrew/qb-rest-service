from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import get_settings
from qbsvc.deps import get_qb_client, reset_token_store_cache
from qbsvc.exceptions import APIError
from qbsvc.main import create_app
from qbsvc.request_context import get_intuit_tid

_TID = "1a2b3c4d-tid"


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    reset_token_store_cache()
    yield
    get_settings.cache_clear()
    reset_token_store_cache()


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_SECRET", "test-secret")


@pytest.fixture
def fresh_store(tmp_path: Path) -> FileTokenStore:
    store = FileTokenStore(path=tmp_path / "tokens.json")
    store.save(
        TokenData(
            access_token="t",
            refresh_token="r",
            realm_id="realm-XYZ",
            expires_at=time.time() + 3600,
        )
    )
    return store


def _client_with(handler, store) -> QBClient:
    c = QBClient(token_store=store, settings=get_settings())
    c._http.close()
    c._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    return c


# ---------- capture: logs + context ----------


def test_qbo_call_log_includes_intuit_tid(caplog, fresh_store):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Customer": {"Id": "1"}}, headers={"intuit_tid": _TID})

    _client_with(handler, fresh_store).get("customer/1")

    rec = [r for r in caplog.records if r.name == "qbsvc.qbo"][-1]
    assert rec.__dict__["intuit_tid"] == _TID


def test_qbo_call_log_omits_tid_when_header_absent(caplog, fresh_store):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Customer": {"Id": "1"}})

    _client_with(handler, fresh_store).get("customer/1")

    rec = [r for r in caplog.records if r.name == "qbsvc.qbo"][-1]
    assert "intuit_tid" not in rec.__dict__


def test_successful_call_binds_tid_to_request_context(fresh_store):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Customer": {"Id": "1"}}, headers={"intuit_tid": _TID})

    _client_with(handler, fresh_store).get("customer/1")

    assert get_intuit_tid() == _TID


# ---------- propagation onto the error path ----------


def test_api_error_carries_intuit_tid(fresh_store):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"Fault": {"Error": [{"Message": "bad", "Detail": "very bad"}]}},
            headers={"intuit_tid": _TID},
        )

    with pytest.raises(APIError) as excinfo:
        _client_with(handler, fresh_store).get("customer/missing")

    assert excinfo.value.intuit_tid == _TID


def test_tid_logged_on_error_response(caplog, fresh_store):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"Fault": {"Error": [{"Message": "bad"}]}},
            headers={"intuit_tid": _TID},
        )

    with pytest.raises(APIError):
        _client_with(handler, fresh_store).get("customer/missing")

    rec = [r for r in caplog.records if r.name == "qbsvc.qbo"][-1]
    assert rec.__dict__["status"] == 400
    assert rec.__dict__["intuit_tid"] == _TID


# ---------- end-to-end: tid in the error envelope ----------


def _route_client(token_store, handler) -> TestClient:
    transport = httpx.MockTransport(handler)
    app = create_app()
    settings = get_settings()

    def fake_dep():
        client = QBClient(token_store=token_store, settings=settings)
        client._http.close()
        client._http = httpx.Client(transport=transport, timeout=30)
        try:
            yield client
        finally:
            client.close()

    app.dependency_overrides[get_qb_client] = fake_dep
    return TestClient(app)


def test_error_envelope_includes_intuit_tid(settings_env, fresh_store):
    def handler(req: httpx.Request) -> httpx.Response:
        # A QBO server fault (not "Object Not Found") → QBO_ERROR 502 envelope.
        return httpx.Response(
            400,
            json={"Fault": {"Error": [{"Message": "boom", "Detail": "upstream blew up"}]}},
            headers={"intuit_tid": _TID},
        )

    client = _route_client(fresh_store, handler)
    resp = client.get("/v1/customers/123")

    assert resp.status_code == 502
    assert resp.json()["error"]["intuit_tid"] == _TID
