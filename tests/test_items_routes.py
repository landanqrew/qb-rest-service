from __future__ import annotations

import base64
import json
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


def _item(id_: str, **fields) -> dict:
    base = {"Id": id_, "Name": f"Item {id_}", "Type": "Service", "Active": True}
    base.update(fields)
    return base


def _query_response(items: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"QueryResponse": {"Item": items, "maxResults": len(items)}},
    )


# ---------- list endpoint ----------


def test_list_default_returns_envelope_with_data_and_pagination(settings_env, token_store):
    def handler(request):
        return _query_response([_item("1"), _item("2")])

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/items")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    assert body["data"][0]["Id"] == "1"
    assert "pagination" in body
    assert body["pagination"]["has_more"] is False


def test_list_queries_item_entity(settings_env, token_store):
    """The SQL must hit `FROM Item`, not Customer or anything else."""
    def handler(request):
        return _query_response([_item("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/items")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "FROM Item" in sql
    assert "Active = true" in sql


def test_active_false_includes_inactive_items(settings_env, token_store):
    """Acceptance: active=false includes inactive items (not just inactive-only)."""
    def handler(request):
        return _query_response(
            [_item("1", Active=True), _item("2", Active=False)]
        )

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/items?active=false")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "Active = true" not in sql
    assert "Active IN (true, false)" in sql or "Active IN (true,false)" in sql


def test_modified_since_translates_to_qbo_metadata_filter(settings_env, token_store):
    """Acceptance: modified_since → MetaData.LastUpdatedTime > 'YYYY-MM-DD'."""
    def handler(request):
        return _query_response([_item("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/items?modified_since=2026-01-01")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "MetaData.LastUpdatedTime > '2026-01-01'" in sql


def test_modified_since_combines_with_active_filter(settings_env, token_store):
    def handler(request):
        return _query_response([_item("1")])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/items?active=true&modified_since=2026-01-01")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "Active = true" in sql
    assert "MetaData.LastUpdatedTime > '2026-01-01'" in sql
    assert " AND " in sql


def test_limit_param_is_propagated_to_qbo(settings_env, token_store):
    """The SQL MAXRESULTS must reflect the requested page size (plus one for has_more sniff)."""
    def handler(request):
        return _query_response([_item(str(i)) for i in range(5)])

    client, recorder = _make_client(token_store, handler)
    resp = client.get("/v1/items?limit=5")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "MAXRESULTS 6" in sql


def test_pagination_round_trip_returns_next_page(settings_env, token_store):
    """Acceptance: follow next_cursor returns next page; has_more=false on last."""
    calls: list[str] = []

    def handler(request):
        sql = parse_qs(urlparse(str(request.url)).query)["query"][0]
        calls.append(sql)
        start = int(sql.split("STARTPOSITION ")[1].split(" ")[0])
        if start == 1:
            return _query_response([_item("1"), _item("2"), _item("3")])
        return _query_response([_item("3")])

    client, _ = _make_client(token_store, handler)

    resp = client.get("/v1/items?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert [c["Id"] for c in body["data"]] == ["1", "2"]
    assert body["pagination"]["has_more"] is True
    cursor = body["pagination"]["next_cursor"]
    assert cursor

    resp2 = client.get(f"/v1/items?limit=2&cursor={cursor}")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert [c["Id"] for c in body2["data"]] == ["3"]
    assert body2["pagination"]["has_more"] is False
    assert body2["pagination"].get("next_cursor") is None


def test_pagination_has_more_false_on_last_page(settings_env, token_store):
    def handler(request):
        return _query_response([_item("1"), _item("2")])

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/items?limit=5")
    body = resp.json()
    assert body["pagination"]["has_more"] is False
    assert body["pagination"].get("next_cursor") in (None,)


def test_cursor_is_opaque_base64(settings_env, token_store):
    def handler(request):
        return _query_response([_item(str(i)) for i in range(1, 4)])  # limit+1

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/items?limit=2")
    cursor = resp.json()["pagination"]["next_cursor"]
    assert not cursor.isdigit()
    padded = cursor + "=" * (-len(cursor) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    assert decoded == "3"


def test_invalid_cursor_returns_400_with_error_envelope(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO should not be hit when the cursor is invalid")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/items?cursor=not-valid-base64!!!")
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
    resp = client.get("/v1/items?modified_since=2026-01-01' OR 'x'='x")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "modified_since" in body["error"]["message"].lower()


# ---------- detail endpoint ----------


def test_detail_returns_envelope_with_item(settings_env, token_store):
    def handler(request):
        assert "item/42" in str(request.url)
        return httpx.Response(200, json={"Item": _item("42")})

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/items/42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["Name"] == "Item 42"


def test_detail_unknown_id_returns_404_with_envelope(settings_env, token_store):
    """Acceptance: unknown id returns 404 with error envelope."""
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
    resp = client.get("/v1/items/9999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]
    assert "Item" in body["error"]["message"]


def test_detail_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/items/42")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"


def test_unauthenticated_returns_503_envelope(settings_env, tmp_path):
    """If no tokens are present, list endpoint surfaces the auth error envelope."""
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.get("/v1/items")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "NOT_AUTHENTICATED"


# ---------- write endpoint helpers ----------


def _post_response(item: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"Item": item, "time": "2026-06-06T10:00:00Z"})


def _item_for_update(id_: str, sync_token: str = "3", **fields) -> dict:
    """An item with a SyncToken, as returned by the GET phase of a sparse
    update. The PUT/DELETE flows read this for the SyncToken before writing.
    """
    return _item(
        id_,
        SyncToken=sync_token,
        IncomeAccountRef={"value": "79", "name": "Sales of Product Income"},
        UnitPrice=75.0,
        **fields,
    )


def _duplicate_name_response() -> httpx.Response:
    """Shape Intuit returns when item-name uniqueness is violated.

    HTTP 400 with Fault.Error[].code=6240. The service maps this to HTTP 409
    with our QBO_DUPLICATE_NAME code so callers can dedupe on it — the same
    pattern the invoice routes use for duplicate DocNumber.
    """
    return httpx.Response(
        400,
        json={
            "Fault": {
                "Error": [
                    {
                        "Message": "Duplicate Name Exists Error",
                        "Detail": (
                            "Duplicate Name Exists Error : The name supplied "
                            "already exists. : Another Item is already using this "
                            "name."
                        ),
                        "code": "6240",
                    }
                ],
                "type": "ValidationFault",
            }
        },
    )


def _stale_sync_token_response() -> httpx.Response:
    """Intuit's "Stale Object Error" — error code 5010 — fired when a sparse
    update's SyncToken doesn't match the current value on the server.
    """
    return httpx.Response(
        400,
        json={
            "Fault": {
                "Error": [
                    {
                        "Message": "Stale Object Error",
                        "Detail": (
                            "Stale Object Error : You and seeddata were updating "
                            "the same Item. Your changes are not saved."
                        ),
                        "code": "5010",
                    }
                ],
                "type": "ValidationFault",
            }
        },
    )


def _not_found_response() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "Fault": {
                "Error": [
                    {
                        "Message": "Object Not Found",
                        "Detail": "Object Not Found",
                        "code": "610",
                    }
                ],
                "type": "ValidationFault",
            }
        },
    )


# ---------- create (POST) ----------


def test_create_returns_201_with_item_envelope(settings_env, token_store):
    """Acceptance: create returns 201 with the created item in the envelope."""
    def handler(request):
        assert request.method == "POST"
        assert "/item" in str(request.url)
        return _post_response(_item("99", Name="Coliform Test"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={
            "name": "Coliform Test",
            "type": "Service",
            "income_account_id": "79",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["Id"] == "99"
    assert body["data"]["Name"] == "Coliform Test"


def test_create_maps_fields_to_qbo_body(settings_env, token_store):
    """name/type/income_account_id/unit_price map to QBO's item shape."""
    def handler(request):
        body = json.loads(request.content)
        assert body["Name"] == "Coliform Test"
        assert body["Type"] == "Service"
        assert body["IncomeAccountRef"] == {"value": "79"}
        assert body["UnitPrice"] == 75.0
        return _post_response(_item("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={
            "name": "Coliform Test",
            "type": "Service",
            "income_account_id": "79",
            "unit_price": 75.0,
        },
    )
    assert resp.status_code == 201


def test_create_omits_absent_unit_price(settings_env, token_store):
    """An absent optional field must not appear in the QBO body."""
    def handler(request):
        body = json.loads(request.content)
        assert "UnitPrice" not in body
        return _post_response(_item("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={"name": "Coliform Test", "type": "Service", "income_account_id": "79"},
    )
    assert resp.status_code == 201


def test_create_duplicate_name_returns_409(settings_env, token_store):
    """Acceptance: duplicate item name (Intuit 6240) → HTTP 409 with a
    QBO_DUPLICATE_* code, mirroring the invoice DocNumber pattern.
    """
    def handler(request):
        return _duplicate_name_response()

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={"name": "Coliform Test", "type": "Service", "income_account_id": "79"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_DUPLICATE_NAME"
    assert "Coliform Test" in body["error"]["message"]
    assert "qbo_detail" in body["error"]
    assert "Duplicate Name" in body["error"]["qbo_detail"]


def test_create_missing_required_field_returns_422(settings_env, token_store):
    """Acceptance: missing required fields return 422 (income_account_id here)."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.post("/v1/items", json={"name": "Coliform Test", "type": "Service"})
    assert resp.status_code == 422


def test_create_unknown_body_field_returns_422(settings_env, token_store):
    """Acceptance: unknown body fields return 422 (strict, extra=forbid)."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={
            "name": "Coliform Test",
            "type": "Service",
            "income_account_id": "79",
            "unitprice": 75.0,  # typo for unit_price
        },
    )
    assert resp.status_code == 422


def test_create_invalid_type_returns_422(settings_env, token_store):
    """type is constrained to Service/Inventory/NonInventory."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={"name": "Coliform Test", "type": "Widget", "income_account_id": "79"},
    )
    assert resp.status_code == 422


def test_create_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={"name": "Coliform Test", "type": "Service", "income_account_id": "79"},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_create_non_duplicate_400_returns_502(settings_env, token_store):
    """A 400 that isn't the duplicate-name fault is a generic upstream error."""
    def handler(request):
        return httpx.Response(
            400,
            json={
                "Fault": {
                    "Error": [
                        {"Message": "Some other validation", "code": "2020"}
                    ],
                    "type": "ValidationFault",
                }
            },
        )

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/items",
        json={"name": "Coliform Test", "type": "Service", "income_account_id": "79"},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_create_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.post(
        "/v1/items",
        json={"name": "Coliform Test", "type": "Service", "income_account_id": "79"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ---------- update (PUT) ----------


def test_update_sparse_updates_and_returns_item(settings_env, token_store):
    """Acceptance: PUT sparse-updates the provided fields and returns the item."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        return _post_response(_item("42", Name="Renamed", UnitPrice=99.0))

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={"name": "Renamed", "unit_price": 99.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["Name"] == "Renamed"


def test_update_sends_sparse_with_id_sync_token_and_only_provided_fields(
    settings_env, token_store
):
    """Acceptance: only the provided fields are written; sparse + SyncToken set."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        body = json.loads(request.content)
        assert body["Id"] == "42"
        assert body["SyncToken"] == "3"
        assert body["sparse"] is True
        assert body["UnitPrice"] == 99.0
        # name/type/income_account were not provided → must be absent.
        assert "Name" not in body
        assert "Type" not in body
        assert "IncomeAccountRef" not in body
        return _post_response(_item("42", UnitPrice=99.0))

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={"unit_price": 99.0})
    assert resp.status_code == 200


def test_update_maps_income_account_id_to_ref(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        body = json.loads(request.content)
        assert body["IncomeAccountRef"] == {"value": "88"}
        return _post_response(_item("42"))

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={"income_account_id": "88"})
    assert resp.status_code == 200


def test_update_stale_sync_token_returns_409(settings_env, token_store):
    """Acceptance: stale SyncToken (Intuit 5010) → HTTP 409."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={"name": "Renamed"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]
    assert "Stale Object" in body["error"]["qbo_detail"]


def test_update_unknown_item_returns_404(settings_env, token_store):
    """Acceptance: not found → 404. The GET phase reports the miss; POST must not fire."""
    def handler(request):
        assert request.method == "GET"
        return _not_found_response()

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/9999", json={"name": "Renamed"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_update_invalid_id_returns_400(settings_env, token_store):
    """Acceptance: invalid (non-numeric) id → 400 INVALID_PARAM, no QBO call."""
    def handler(request):
        pytest.fail("QBO must not be hit for a malformed item id")

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/0;DROP", json={"name": "Renamed"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "item_id" in body["error"]["message"].lower()


def test_update_unknown_body_field_returns_422(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={"naem": "Renamed"})
    assert resp.status_code == 422


def test_update_empty_body_returns_422(settings_env, token_store):
    """A sparse update with no fields is a no-op; reject it before hitting QBO."""
    def handler(request):
        pytest.fail("QBO must not be hit for an empty sparse update")

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={})
    assert resp.status_code == 422


def test_update_duplicate_name_returns_409(settings_env, token_store):
    """Renaming an item onto an existing name also surfaces Intuit 6240."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        return _duplicate_name_response()

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/items/42", json={"name": "Taken"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "QBO_DUPLICATE_NAME"


def test_update_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.put("/v1/items/42", json={"name": "Renamed"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ---------- deactivate (DELETE) ----------


def test_delete_sets_active_false_and_returns_item(settings_env, token_store):
    """Acceptance: DELETE sets Active:false and returns the deactivated item."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        body = json.loads(request.content)
        assert body["Id"] == "42"
        assert body["SyncToken"] == "3"
        assert body["sparse"] is True
        assert body["Active"] is False
        return _post_response(_item("42", Active=False))

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/items/42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["Active"] is False


def test_delete_already_inactive_passes_through(settings_env, token_store):
    """Deactivating an already-inactive item passes QBO's response through
    rather than masking it (Amendment 1 idempotency note).
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"Item": _item_for_update("42", Active=False)}
            )
        return _post_response(_item("42", Active=False))

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/items/42")
    assert resp.status_code == 200
    assert resp.json()["data"]["Active"] is False


def test_delete_stale_sync_token_returns_409(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Item": _item_for_update("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/items/42")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "QBO_STALE_SYNC_TOKEN"


def test_delete_unknown_item_returns_404(settings_env, token_store):
    def handler(request):
        assert request.method == "GET"
        return _not_found_response()

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/items/9999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_delete_invalid_id_returns_400(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for a malformed item id")

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/items/not-a-number")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PARAM"
