from __future__ import annotations

import base64
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
    get_token_store,
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
    """Captures the QBO requests intercepted by httpx.MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def last_query(self) -> str:
        req = self.requests[-1]
        qs = parse_qs(urlparse(str(req.url)).query)
        return qs["query"][0]


def _make_client(token_store, handler) -> tuple[TestClient, _Recorder]:
    """Build a TestClient whose QBClient is wired to an httpx.MockTransport.

    The recorder lets tests assert on the exact QBO SQL queries that hit the
    fake upstream — that's how we prove the route translates inputs correctly.
    """
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


def _customer(id_: str, **fields) -> dict:
    base = {"Id": id_, "DisplayName": f"Customer {id_}", "Active": True}
    base.update(fields)
    return base


def _query_response(customers: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"QueryResponse": {"Customer": customers, "maxResults": len(customers)}},
    )


# ---------- list endpoint ----------


def test_list_default_returns_envelope_with_data_and_pagination(settings_env, token_store):
    def handler(request):
        return _query_response([_customer("1"), _customer("2")])

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    assert body["data"][0]["Id"] == "1"
    assert "pagination" in body
    assert body["pagination"]["has_more"] is False


def test_list_default_filters_to_active_customers(settings_env, token_store):
    def handler(request):
        return _query_response([_customer("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/customers")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "Active = true" in sql
    assert "FROM Customer" in sql


def test_active_false_includes_inactive_customers(settings_env, token_store):
    """Acceptance: active=false includes inactive customers (not just inactive-only)."""
    def handler(request):
        return _query_response(
            [_customer("1", Active=True), _customer("2", Active=False)]
        )

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/customers?active=false")
    assert resp.status_code == 200
    sql = recorder.last_query
    # The SQL must NOT pin Active=true — that would silently drop inactive rows.
    assert "Active = true" not in sql
    # And it must explicitly include both states (QBO's default is Active=true).
    assert "Active IN (true, false)" in sql or "Active IN (true,false)" in sql


def test_modified_since_translates_to_qbo_metadata_filter(settings_env, token_store):
    """Acceptance: modified_since → MetaData.LastUpdatedTime > 'YYYY-MM-DD'."""
    def handler(request):
        return _query_response([_customer("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/customers?modified_since=2026-01-01")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "MetaData.LastUpdatedTime > '2026-01-01'" in sql


def test_modified_since_combines_with_active_filter(settings_env, token_store):
    def handler(request):
        return _query_response([_customer("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/customers?active=true&modified_since=2026-01-01")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "Active = true" in sql
    assert "MetaData.LastUpdatedTime > '2026-01-01'" in sql
    assert " AND " in sql


def test_limit_param_is_propagated_to_qbo(settings_env, token_store):
    """The SQL MAXRESULTS must reflect the requested page size (plus one for has_more sniff)."""
    def handler(request):
        return _query_response([_customer(str(i)) for i in range(5)])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/customers?limit=5")
    assert resp.status_code == 200
    sql = recorder.last_query
    # We fetch limit+1 to detect has_more without a second round-trip.
    assert "MAXRESULTS 6" in sql


def test_pagination_round_trip_returns_next_page(settings_env, token_store):
    """Acceptance: follow next_cursor returns next page; has_more=false on last."""
    calls: list[str] = []

    def handler(request):
        sql = parse_qs(urlparse(str(request.url)).query)["query"][0]
        calls.append(sql)
        start = int(sql.split("STARTPOSITION ")[1].split(" ")[0])
        # Page 1 (start=1): return limit+1=3 items → has_more=true, trim to 2.
        # Page 2 (start=3): return 1 item < limit+1=3 → has_more=false.
        if start == 1:
            return _query_response([_customer("1"), _customer("2"), _customer("3")])
        return _query_response([_customer("3")])

    client, _ = _make_client(token_store, handler)

    resp = client.get("/v1/customers?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["Id"] for c in body["data"]] == ["1", "2"]
    assert body["pagination"]["has_more"] is True
    cursor = body["pagination"]["next_cursor"]
    assert cursor

    resp2 = client.get(f"/v1/customers?limit=2&cursor={cursor}")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert [c["Id"] for c in body2["data"]] == ["3"]
    assert body2["pagination"]["has_more"] is False
    assert body2["pagination"].get("next_cursor") is None


def test_pagination_has_more_false_on_last_page(settings_env, token_store):
    """When QBO returns fewer items than the page-size sniff, has_more must be false."""
    def handler(request):
        return _query_response([_customer("1"), _customer("2")])

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers?limit=5")
    body = resp.json()
    assert body["pagination"]["has_more"] is False
    assert body["pagination"].get("next_cursor") in (None,)


def test_pagination_next_cursor_advances_startposition(settings_env, token_store):
    """The cursor returned on page 1 must decode to STARTPOSITION = page1_size + 1."""
    captured = {}

    def handler(request):
        sql = parse_qs(urlparse(str(request.url)).query)["query"][0]
        if "calls" not in captured:
            captured["calls"] = []
        captured["calls"].append(sql)
        # First call returns 3 (limit+1) → has_more, page = first 2.
        if len(captured["calls"]) == 1:
            return _query_response([_customer("1"), _customer("2"), _customer("3")])
        # Second call must come back at STARTPOSITION 3.
        return _query_response([_customer("3")])

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers?limit=2")
    assert resp.status_code == 200
    cursor = resp.json()["pagination"]["next_cursor"]
    assert cursor

    resp2 = client.get(f"/v1/customers?limit=2&cursor={cursor}")
    assert resp2.status_code == 200
    # Second SQL had STARTPOSITION 3.
    assert "STARTPOSITION 3" in captured["calls"][1]


def test_cursor_is_opaque_base64(settings_env, token_store):
    """Cursor is base64; clients should not be able to read it as a plain integer."""
    def handler(request):
        return _query_response([_customer(str(i)) for i in range(1, 4)])  # limit+1

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers?limit=2")
    cursor = resp.json()["pagination"]["next_cursor"]
    # Not a bare integer; decodes to one.
    assert not cursor.isdigit()
    padded = cursor + "=" * (-len(cursor) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    assert decoded == "3"


def test_invalid_cursor_returns_400_with_error_envelope(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO should not be hit when the cursor is invalid")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers?cursor=not-valid-base64!!!")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_CURSOR"


def test_malformed_modified_since_returns_400_without_hitting_qbo(
    settings_env, token_store
):
    """Regression guard for SQL injection: anything not strictly YYYY-MM-DD
    must be rejected before being interpolated into QBO SQL.
    """

    def handler(request):
        pytest.fail("QBO must not be hit when modified_since is malformed")

    client, _ = _make_client(token_store, handler)
    # Classic injection attempt — closes the date literal, adds a tautology.
    resp = client.get("/v1/customers?modified_since=2026-01-01' OR 'x'='x")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "modified_since" in body["error"]["message"].lower()


def test_modified_since_rejects_semantically_invalid_dates(settings_env, token_store):
    """Feb 30 has the right shape but isn't a real date — reject up front
    so callers see a clean 400 rather than an obscure QBO error.
    """

    def handler(request):
        pytest.fail("QBO must not be hit for an invalid calendar date")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers?modified_since=2026-02-30")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAM"


def test_modified_since_rejects_extended_iso_formats(settings_env, token_store):
    """date.fromisoformat in 3.11+ accepts ordinal/week-date forms; we want
    strict YYYY-MM-DD only so the SQL literal is unambiguous.
    """

    def handler(request):
        pytest.fail("QBO must not be hit for non-YYYY-MM-DD inputs")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers?modified_since=20260101")  # basic ISO, no dashes
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAM"


# ---------- detail endpoint ----------


def test_detail_returns_envelope_with_customer(settings_env, token_store):
    def handler(request):
        assert "customer/42" in str(request.url)
        return httpx.Response(200, json={"Customer": _customer("42")})

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers/42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["DisplayName"] == "Customer 42"


def test_detail_unknown_id_returns_404_with_envelope(settings_env, token_store):
    """Acceptance: unknown id returns 404 with error envelope."""
    def handler(request):
        # QBO's actual shape for a missing object: HTTP 400, Fault.Error code 610.
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
    resp = client.get("/v1/customers/9999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_detail_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/customers/42")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"


def test_unauthenticated_returns_503_envelope(settings_env, tmp_path):
    """If no tokens are present, list endpoint surfaces the auth error envelope."""
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.get("/v1/customers")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "NOT_AUTHENTICATED"
