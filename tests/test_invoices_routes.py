from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import get_settings
from qbsvc.deps import (
    get_qb_client,
    reset_token_store_cache,
)
from qbsvc.main import create_app


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
def token_store(tmp_path: Path):
    store = FileTokenStore(path=tmp_path / "tokens.json")
    store.save(
        TokenData(
            access_token="fresh-access",
            refresh_token="fresh-refresh",
            realm_id="realm-123",
            expires_at=time.time() + 3600,
        )
    )
    return store


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def last_query(self) -> str:
        req = self.requests[-1]
        qs = parse_qs(urlparse(str(req.url)).query)
        return qs["query"][0]


def _make_client(token_store, handler) -> tuple[TestClient, _Recorder]:
    recorder = _Recorder()

    def wrapped(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
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
    return TestClient(app), recorder


def _invoice(id_: str, **fields) -> dict:
    base = {
        "Id": id_,
        "DocNumber": f"26-02-{id_}",
        "CustomerRef": {"value": "55", "name": "ACME Labs"},
        "TxnDate": "2026-02-15",
        "TotalAmt": 100.0,
    }
    base.update(fields)
    return base


def _invoice_with_lines(id_: str) -> dict:
    """An invoice whose Line array exercises both an item line and the
    SubTotalLineDetail row QBO appends to every invoice.
    """
    return _invoice(
        id_,
        Line=[
            {
                "Id": "1",
                "LineNum": 1,
                "Amount": 75.0,
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "9", "name": "Coliform Test"},
                    "Qty": 1,
                    "UnitPrice": 75.0,
                },
            },
            {
                "Amount": 75.0,
                "DetailType": "SubTotalLineDetail",
                "SubTotalLineDetail": {},
            },
        ],
    )


def _query_response(invoices: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"QueryResponse": {"Invoice": invoices, "maxResults": len(invoices)}},
    )


# ---------- list endpoint ----------


def test_list_default_returns_envelope_with_data_and_pagination(settings_env, token_store):
    def handler(request):
        return _query_response([_invoice("1"), _invoice("2")])

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    assert body["data"][0]["Id"] == "1"
    assert "pagination" in body
    assert body["pagination"]["has_more"] is False


def test_list_queries_invoice_entity(settings_env, token_store):
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/invoices")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "FROM Invoice" in sql


def test_list_no_filters_omits_where_clause(settings_env, token_store):
    """Invoice has no Active field; with no filters the SQL must not have a
    dangling WHERE that QBO would reject."""
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/invoices")
    assert resp.status_code == 200
    sql = recorder.last_query
    # No filters provided → no WHERE clause.
    assert " WHERE " not in sql
    # Pagination markers still present.
    assert "STARTPOSITION 1" in sql
    assert "MAXRESULTS" in sql


def test_customer_id_translates_to_customer_ref_filter(settings_env, token_store):
    """Acceptance: customer_id → CustomerRef = '<id>'."""
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?customer_id=55")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "CustomerRef = '55'" in sql


def test_doc_number_translates_to_doc_number_filter(settings_env, token_store):
    """Acceptance: doc_number → DocNumber = '<n>'."""
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?doc_number=26-02-71")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "DocNumber = '26-02-71'" in sql


def test_filters_combinable(settings_env, token_store):
    """Acceptance: customer_id, doc_number, modified_since combinable via AND."""
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get(
        "/v1/invoices?customer_id=55&doc_number=26-02-71&modified_since=2026-01-01"
    )
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "CustomerRef = '55'" in sql
    assert "DocNumber = '26-02-71'" in sql
    assert "MetaData.LastUpdatedTime > '2026-01-01'" in sql
    assert sql.count(" AND ") == 2


def test_doc_number_is_exact_match_not_like(settings_env, token_store):
    """Acceptance: doc_number exact match, not LIKE / prefix."""
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?doc_number=26-02-71")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "LIKE" not in sql.upper().replace("LIKELY", "")
    assert "%" not in sql


def test_invalid_customer_id_rejected_without_hitting_qbo(settings_env, token_store):
    """SQL injection guard: anything not strictly digits is rejected up front."""
    def handler(request):
        pytest.fail("QBO must not be hit for malformed customer_id")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?customer_id=55' OR '1'='1")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "customer_id" in body["error"]["message"].lower()


def test_invalid_doc_number_rejected_without_hitting_qbo(settings_env, token_store):
    """SQL injection guard: doc_number containing a single quote is rejected."""
    def handler(request):
        pytest.fail("QBO must not be hit for malformed doc_number")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?doc_number=26'%20OR%20'1'='1")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "doc_number" in body["error"]["message"].lower()


def test_limit_param_is_propagated_to_qbo(settings_env, token_store):
    def handler(request):
        return _query_response([_invoice(str(i)) for i in range(5)])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?limit=5")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "MAXRESULTS 6" in sql


def test_pagination_round_trip_returns_next_page(settings_env, token_store):
    def handler(request):
        sql = parse_qs(urlparse(str(request.url)).query)["query"][0]
        start = int(sql.split("STARTPOSITION ")[1].split(" ")[0])
        if start == 1:
            return _query_response([_invoice("1"), _invoice("2"), _invoice("3")])
        return _query_response([_invoice("3")])

    client, _ = _make_client(token_store, handler)

    resp = client.get("/v1/invoices?limit=2")
    body = resp.json()
    assert [c["Id"] for c in body["data"]] == ["1", "2"]
    assert body["pagination"]["has_more"] is True
    cursor = body["pagination"]["next_cursor"]
    assert cursor

    resp2 = client.get(f"/v1/invoices?limit=2&cursor={cursor}")
    body2 = resp2.json()
    assert [c["Id"] for c in body2["data"]] == ["3"]
    assert body2["pagination"]["has_more"] is False


def test_invalid_cursor_returns_400(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO should not be hit when the cursor is invalid")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?cursor=not-valid-base64!!!")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_CURSOR"


def test_malformed_modified_since_returns_400(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed modified_since")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices?modified_since=2026-01-01' OR 'x'='x")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAM"


# ---------- detail endpoint ----------


def test_detail_returns_invoice_with_full_line_array(settings_env, token_store):
    """Acceptance: detail response includes full Line array including SubTotalLineDetail."""
    def handler(request):
        assert "invoice/42" in str(request.url)
        return httpx.Response(200, json={"Invoice": _invoice_with_lines("42")})

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices/42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    lines = body["data"]["Line"]
    assert len(lines) == 2
    detail_types = [line["DetailType"] for line in lines]
    assert "SalesItemLineDetail" in detail_types
    assert "SubTotalLineDetail" in detail_types
    subtotal = next(l for l in lines if l["DetailType"] == "SubTotalLineDetail")
    assert "SubTotalLineDetail" in subtotal


def test_detail_unknown_id_returns_404(settings_env, token_store):
    def handler(request):
        return httpx.Response(
            400,
            json={
                "Fault": {
                    "Error": [
                        {
                            "Message": "Object Not Found",
                            "Detail": "Object Not Found: Something you're trying to find doesn't exist.",
                            "code": "610",
                        }
                    ],
                    "type": "ValidationFault",
                }
            },
        )

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices/9999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]
    assert "Invoice" in body["error"]["message"]


def test_detail_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices/42")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.get("/v1/invoices")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"
