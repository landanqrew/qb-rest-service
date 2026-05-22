from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import pytest

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings
from qbsvc.exceptions import APIError
from qbsvc.logging import configure_logging, reset_logging_for_tests
from qbsvc.request_context import reset_request_id, set_request_id


@pytest.fixture
def settings() -> Settings:
    return Settings()


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


@pytest.fixture
def qbo_log_records(caplog) -> list[logging.LogRecord]:
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")
    return caplog.records


def _client_with(transport: httpx.MockTransport, store: FileTokenStore, settings):
    c = QBClient(token_store=store, settings=settings)
    c._http.close()
    c._http = httpx.Client(transport=transport, timeout=30)
    return c


def test_qbo_get_emits_structured_log_line(
    caplog, fresh_store, settings
):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Customer": {"Id": "1"}})

    client = _client_with(httpx.MockTransport(handler), fresh_store, settings)
    client.get("customer/1")

    qbo_records = [r for r in caplog.records if r.name == "qbsvc.qbo"]
    assert len(qbo_records) == 1
    rec = qbo_records[0]
    assert rec.__dict__["method"] == "GET"
    assert rec.__dict__["qbo_endpoint"] == "customer/1"
    assert rec.__dict__["status"] == 200
    assert rec.__dict__["realm_id"] == "realm-XYZ"
    assert isinstance(rec.__dict__["qbo_duration_ms"], (int, float))


def test_qbo_query_logs_query_endpoint(caplog, fresh_store, settings):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"QueryResponse": {"Customer": []}})

    client = _client_with(httpx.MockTransport(handler), fresh_store, settings)
    client.query("SELECT * FROM Customer")

    qbo = [r for r in caplog.records if r.name == "qbsvc.qbo"]
    assert qbo and qbo[0].__dict__["qbo_endpoint"] == "query"
    assert qbo[0].__dict__["status"] == 200


def test_qbo_log_includes_request_id_when_set(caplog, fresh_store, settings):
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client_with(httpx.MockTransport(handler), fresh_store, settings)
    token = set_request_id("req-from-test")
    try:
        client.get("companyinfo/realm-XYZ")
    finally:
        reset_request_id(token)

    qbo = [r for r in caplog.records if r.name == "qbsvc.qbo"]
    # The formatter pulls request_id from the ContextVar; on the record the
    # field may or may not be present, but logger.info was called inside the
    # set context. We assert via the JSON formatter the same way the prod
    # path does.
    from qbsvc.logging import JsonFormatter

    formatter = JsonFormatter()
    # Re-set ContextVar so formatter sees the right value at format time —
    # mirrors the production path where the log line is formatted in the
    # request context.
    token2 = set_request_id("req-from-test")
    try:
        payload = json.loads(formatter.format(qbo[0]))
    finally:
        reset_request_id(token2)
    assert payload["request_id"] == "req-from-test"
    assert payload["qbo_endpoint"] == "companyinfo/realm-XYZ"
    assert payload["realm_id"] == "realm-XYZ"


def test_qbo_log_emitted_even_when_request_fails(caplog, fresh_store, settings):
    """Acceptance: every QBO call produces a structured log line — even errors."""
    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"Fault": {"Error": [{"Message": "bad", "Detail": "very bad"}]}}
        )

    client = _client_with(httpx.MockTransport(handler), fresh_store, settings)
    with pytest.raises(APIError):
        client.get("customer/missing")

    qbo = [r for r in caplog.records if r.name == "qbsvc.qbo"]
    assert qbo, "QBO log line must be emitted even on error responses"
    assert qbo[-1].__dict__["status"] == 400


def test_qbo_log_emitted_when_401_refresh_raises(
    caplog, fresh_store, settings, monkeypatch
):
    """A 401 from QBO triggers a token refresh; if the refresh blows up, the
    original 401 must still be logged so an operator can correlate the failed
    QBO call to whatever happened in token-refresh logs.
    """
    from qbsvc.exceptions import TokenRefreshError

    caplog.set_level(logging.INFO, logger="qbsvc.qbo")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"Fault": {"Error": [{"Message": "expired"}]}})

    client = _client_with(httpx.MockTransport(handler), fresh_store, settings)

    def boom(*_a, **_kw):
        raise TokenRefreshError("invalid_grant")

    monkeypatch.setattr(client, "_refresh_tokens", boom)

    with pytest.raises(TokenRefreshError):
        client.get("customer/1")

    qbo = [r for r in caplog.records if r.name == "qbsvc.qbo"]
    assert qbo, "401 attempt must be logged before the refresh raises"
    assert qbo[-1].__dict__["status"] == 401
    assert qbo[-1].__dict__["qbo_endpoint"] == "customer/1"


@pytest.fixture(autouse=True)
def _reset_logging():
    yield
    reset_logging_for_tests()
