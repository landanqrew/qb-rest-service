from __future__ import annotations

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


def test_doc_number_is_stripped_before_matching(settings_env, token_store):
    """A padded query-string value must not silently zero-match against QBO's
    unpadded stored DocNumber. Strip leading/trailing whitespace before
    interpolating into the SQL literal.
    """
    def handler(request):
        return _query_response([_invoice("1")])

    client, recorder = _make_client(token_store, handler)
    # %20 = space; client passes "  26-02-71  ".
    resp = client.get("/v1/invoices?doc_number=%20%2026-02-71%20%20")
    assert resp.status_code == 200
    sql = recorder.last_query
    assert "DocNumber = '26-02-71'" in sql
    assert "' 26" not in sql
    assert "71 '" not in sql


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


def test_detail_invalid_id_rejected_without_hitting_qbo(settings_env, token_store):
    """Defence-in-depth: a non-numeric invoice_id must produce a clean 400
    rather than an opaque QBO error.
    """
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.get("/v1/invoices/0;%20DROP%20TABLE")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


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


# ---------- create endpoint ----------


def _post_response(invoice: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"Invoice": invoice, "time": "2026-02-15T10:00:00Z"})


def _duplicate_doc_number_response() -> httpx.Response:
    """Shape Intuit returns when DocNumber uniqueness is violated.

    HTTP 400 with Fault.Error[].code=6240. The service maps this to HTTP 409
    with our QBO_DUPLICATE_DOCNUMBER code so callers can dedupe on it.
    """
    return httpx.Response(
        400,
        json={
            "Fault": {
                "Error": [
                    {
                        "Message": "Duplicate Document Number Exists",
                        "Detail": (
                            "Duplicate Document Number Exists. "
                            "Another customer transaction is already using this number."
                        ),
                        "code": "6240",
                    }
                ],
                "type": "ValidationFault",
            }
        },
    )


def test_create_returns_201_with_invoice_envelope(settings_env, token_store):
    """Acceptance: sandbox create returns 201 with invoice body."""
    def handler(request):
        assert request.method == "POST"
        assert "/invoice" in str(request.url)
        return _post_response(_invoice("99", DocNumber="26-02-0099"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["Id"] == "99"
    assert body["data"]["DocNumber"] == "26-02-0099"


def test_create_posts_customer_ref_and_doc_number_to_qbo(settings_env, token_store):
    """Minimum required fields are mapped to QBO's CustomerRef + DocNumber."""
    def handler(request):
        body = json.loads(request.content)
        assert body["CustomerRef"] == {"value": "55"}
        assert body["DocNumber"] == "26-02-0099"
        # txn_date / memo / lines are absent — must not appear in the QBO body.
        assert "TxnDate" not in body
        assert "PrivateNote" not in body
        assert "CustomerMemo" not in body
        assert "Line" not in body
        return _post_response(_invoice("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099"},
    )
    assert resp.status_code == 201


def test_create_with_empty_lines_omits_line_array(settings_env, token_store):
    """Phase 3 of the Lab Intake flow creates the invoice empty; passing
    `lines: []` must not send a `Line: []` to QBO (QBO rejects empty Line
    arrays on Invoice create). An absent Line key is correct.
    """
    def handler(request):
        body = json.loads(request.content)
        assert "Line" not in body
        return _post_response(_invoice("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099", "lines": []},
    )
    assert resp.status_code == 201


def test_create_translates_lines_into_qbo_sales_item_shape(settings_env, token_store):
    """Each input line maps to a SalesItemLineDetail with ItemRef/Qty/UnitPrice,
    and the line's Amount is computed as qty * rate.
    """
    def handler(request):
        body = json.loads(request.content)
        assert len(body["Line"]) == 1
        line = body["Line"][0]
        assert line["DetailType"] == "SalesItemLineDetail"
        assert line["Amount"] == 150.0  # 2 * 75
        assert line["Description"] == "Coliform"
        detail = line["SalesItemLineDetail"]
        assert detail["ItemRef"] == {"value": "9"}
        assert detail["Qty"] == 2
        assert detail["UnitPrice"] == 75.0
        return _post_response(_invoice_with_lines("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0099",
            "lines": [
                {
                    "item_id": "9",
                    "qty": 2,
                    "rate": 75.0,
                    "description": "Coliform",
                }
            ],
        },
    )
    assert resp.status_code == 201


def test_create_passes_optional_txn_date_and_memos(settings_env, token_store):
    def handler(request):
        body = json.loads(request.content)
        assert body["TxnDate"] == "2026-02-15"
        assert body["PrivateNote"] == "internal note"
        assert body["CustomerMemo"] == {"value": "thanks!"}
        return _post_response(_invoice("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0099",
            "txn_date": "2026-02-15",
            "memo": "internal note",
            "customer_memo": "thanks!",
        },
    )
    assert resp.status_code == 201


def test_create_duplicate_doc_number_returns_409(settings_env, token_store):
    """Acceptance: Intuit error 6240 (Duplicate Document Number) → HTTP 409
    with code QBO_DUPLICATE_DOCNUMBER. Lab Intake retries with the same
    deterministic DocNumber, so 409 is the natural-dedup signal.
    """
    def handler(request):
        return _duplicate_doc_number_response()

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_DUPLICATE_DOCNUMBER"
    # Message names the offending DocNumber so the caller can correlate.
    assert "26-02-0099" in body["error"]["message"]
    # QBO's own detail is preserved for debugging.
    assert "qbo_detail" in body["error"]
    assert "Duplicate Document Number" in body["error"]["qbo_detail"]


def test_create_missing_required_field_returns_422(settings_env, token_store):
    """Acceptance: missing required fields return 422."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    # doc_number is required.
    resp = client.post("/v1/invoices", json={"customer_id": "55"})
    assert resp.status_code == 422


def test_create_unknown_body_field_returns_422(settings_env, token_store):
    """Acceptance: unknown body fields return 422 (strict, extra=forbid).

    The web app must hear about typos / misnamed fields loudly rather than
    have them silently dropped on the floor.
    """
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0099",
            "tnx_date": "2026-02-15",  # typo for txn_date
        },
    )
    assert resp.status_code == 422


def test_create_unknown_line_field_returns_422(settings_env, token_store):
    """Strict validation applies to nested line objects too."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0099",
            "lines": [
                {
                    "item_id": "9",
                    "qty": 1,
                    "rate": 75.0,
                    "unit_price": 75.0,  # not in our schema
                }
            ],
        },
    )
    assert resp.status_code == 422


def test_create_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099"},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_create_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_create_line_without_rate_returns_422(settings_env, token_store):
    """A line with qty but no rate must be rejected at validation time (422),
    not sent to QBO where the missing Amount would surface as an opaque 502.
    Rate-less lines belong on the append flow (#9).
    """
    def handler(request):
        pytest.fail("QBO must not be hit when a line is missing its rate")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0099",
            "lines": [{"item_id": "9", "qty": 1}],  # rate absent
        },
    )
    assert resp.status_code == 422


def test_create_doc_number_too_long_returns_422(settings_env, token_store):
    """QBO truncates DocNumber at 21 chars; surface the limit at the API
    boundary instead of silently storing a clipped value.
    """
    def handler(request):
        pytest.fail("QBO must not be hit when doc_number exceeds 21 chars")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "x" * 22},
    )
    assert resp.status_code == 422


def test_create_empty_doc_number_returns_422(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit when doc_number is empty")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": ""},
    )
    assert resp.status_code == 422


def test_create_amount_is_rounded_to_two_decimal_places(settings_env, token_store):
    """`Amount = qty * rate` rounds at the API boundary so float artefacts
    like 3 * 0.10 = 0.30000000000000004 never reach QBO's decimal(12,2)
    Amount field.
    """
    def handler(request):
        body = json.loads(request.content)
        amount = body["Line"][0]["Amount"]
        assert amount == 0.30
        return _post_response(_invoice("99"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0099",
            "lines": [{"item_id": "9", "qty": 3, "rate": 0.10}],
        },
    )
    assert resp.status_code == 201


def test_create_non_duplicate_400_returns_502(settings_env, token_store):
    """Defence in depth: a QBO 400 that is NOT the duplicate-DocNumber error
    must not be misclassified as 409. We only specialize on code 6240.
    """
    def handler(request):
        return httpx.Response(
            400,
            json={
                "Fault": {
                    "Error": [
                        {
                            "Message": "Required param missing",
                            "Detail": "Required param missing.",
                            "code": "2020",
                        }
                    ]
                }
            },
        )

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices",
        json={"customer_id": "55", "doc_number": "26-02-0099"},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


# ---------- line append endpoint ----------


def _invoice_for_append(id_: str, sync_token: str = "3") -> dict:
    """An invoice that already has one item line and the QBO-appended
    SubTotalLineDetail row. The append flow must preserve the item line,
    drop the subtotal row, and add a new line before sparse-updating.
    """
    return _invoice(
        id_,
        SyncToken=sync_token,
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


def _stale_sync_token_response() -> httpx.Response:
    """Intuit's "Stale Object Error" — error code 5010 — fired when a sparse
    update's SyncToken doesn't match the current value on the server. Two
    concurrent appends race here: the loser sees this response.
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
                            "the same Invoice. Your changes are not saved."
                        ),
                        "code": "5010",
                    }
                ],
                "type": "ValidationFault",
            }
        },
    )


def test_append_line_succeeds_and_returns_updated_invoice(settings_env, token_store):
    """Acceptance: append succeeds; total recomputed by QBO. The route does
    GET → strip SubTotal → append → sparse POST, and returns the updated
    invoice in the standard envelope.
    """
    updated = _invoice(
        "42",
        SyncToken="4",
        Line=[
            {
                "Id": "1",
                "DetailType": "SalesItemLineDetail",
                "Amount": 75.0,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "9"},
                    "Qty": 1,
                    "UnitPrice": 75.0,
                },
            },
            {
                "Id": "2",
                "DetailType": "SalesItemLineDetail",
                "Amount": 150.0,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "11"},
                    "Qty": 2,
                    "UnitPrice": 75.0,
                },
            },
            {
                "Amount": 225.0,
                "DetailType": "SubTotalLineDetail",
                "SubTotalLineDetail": {},
            },
        ],
        TotalAmt=225.0,
    )

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        assert request.method == "POST"
        return _post_response(updated)

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 2, "rate": 75.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["TotalAmt"] == 225.0
    assert len(body["data"]["Line"]) == 3


def test_append_line_sends_sparse_update_with_id_and_sync_token(settings_env, token_store):
    """The POST body must carry Id, SyncToken, sparse=true, and the full
    Line array (existing lines minus SubTotal, plus the appended line).
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42", sync_token="7")})
        body = json.loads(request.content)
        assert body["Id"] == "42"
        assert body["SyncToken"] == "7"
        assert body["sparse"] is True
        # Existing item line preserved; SubTotal stripped; new line appended.
        assert len(body["Line"]) == 2
        # Subtotal row must NOT appear in the outbound body — QBO regenerates it.
        detail_types = [line["DetailType"] for line in body["Line"]]
        assert "SubTotalLineDetail" not in detail_types
        # Original line kept verbatim.
        assert body["Line"][0]["Id"] == "1"
        # New line at the end, with the appended item/qty/rate.
        new_line = body["Line"][-1]
        assert new_line["DetailType"] == "SalesItemLineDetail"
        assert new_line["Amount"] == 150.0  # 2 * 75
        assert new_line["SalesItemLineDetail"]["ItemRef"] == {"value": "11"}
        assert new_line["SalesItemLineDetail"]["Qty"] == 2
        assert new_line["SalesItemLineDetail"]["UnitPrice"] == 75.0
        return _post_response(_invoice_for_append("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 2, "rate": 75.0},
    )
    assert resp.status_code == 200


def test_append_line_without_rate_omits_unit_price_and_amount(settings_env, token_store):
    """Acceptance: rate-omitted line uses item's default price (QBO computes
    Amount). The outbound line must therefore omit UnitPrice AND Amount so
    QBO falls back to the item's stored price.
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        body = json.loads(request.content)
        new_line = body["Line"][-1]
        assert "Amount" not in new_line
        assert "UnitPrice" not in new_line["SalesItemLineDetail"]
        assert new_line["SalesItemLineDetail"]["ItemRef"] == {"value": "11"}
        assert new_line["SalesItemLineDetail"]["Qty"] == 1
        return _post_response(_invoice_for_append("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1},  # no rate
    )
    assert resp.status_code == 200


def test_append_line_passes_description_when_present(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        body = json.loads(request.content)
        assert body["Line"][-1]["Description"] == "rush job"
        return _post_response(_invoice_for_append("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 50.0, "description": "rush job"},
    )
    assert resp.status_code == 200


def test_append_line_stale_sync_token_returns_409(settings_env, token_store):
    """Acceptance: two concurrent appends in sandbox — one wins, the other
    gets 409. Intuit signals the race with error code 5010 (Stale Object
    Error); the service maps it to HTTP 409 with QBO_STALE_SYNC_TOKEN so the
    caller can re-fetch and retry deterministically.
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]
    assert "qbo_detail" in body["error"]
    assert "Stale Object" in body["error"]["qbo_detail"]


def test_append_line_unknown_invoice_returns_404(settings_env, token_store):
    """If the GET phase reports the invoice doesn't exist, propagate 404."""
    def handler(request):
        assert request.method == "GET"  # POST must not fire
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

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/9999/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_append_line_invalid_invoice_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/0;DROP/lines",
        json={"item_id": "11", "qty": 1},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


def test_append_line_missing_required_field_returns_422(settings_env, token_store):
    """Project-wide envelope handler reshapes RequestValidationError into
    422 + VALIDATION_ERROR (errors.py:_validation). Append flow inherits
    that contract — QBO must not be hit when the body is malformed.
    """
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    # qty is required.
    resp = client.post("/v1/invoices/42/lines", json={"item_id": "11"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_append_line_unknown_body_field_returns_422(settings_env, token_store):
    """Strict validation — typos must be loud, not silently dropped."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0, "unit_price": 75.0},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_append_line_amount_is_rounded_to_two_decimal_places(settings_env, token_store):
    """Same float-artefact guard as the create path."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        body = json.loads(request.content)
        assert body["Line"][-1]["Amount"] == 0.30
        return _post_response(_invoice_for_append("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 3, "rate": 0.10},
    )
    assert resp.status_code == 200


def test_append_line_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_append_line_non_stale_400_returns_502(settings_env, token_store):
    """Only error code 5010 maps to 409. Other 400s stay as 502 QBO_ERROR."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return httpx.Response(
            400,
            json={
                "Fault": {
                    "Error": [
                        {
                            "Message": "Required param missing",
                            "Detail": "Required param missing.",
                            "code": "2020",
                        }
                    ]
                }
            },
        )

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_append_line_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_append_line_partial_invoice_body_returns_502(settings_env, token_store):
    """Defence in depth: if QBO returns a 200 with an Invoice object missing
    Id or SyncToken (beta-API quirk / partial response), the route must emit
    a clean 502 rather than crash with a KeyError into an unhandled 500.
    """
    def handler(request):
        assert request.method == "GET"  # POST must not fire without a SyncToken
        return httpx.Response(
            200,
            json={"Invoice": {"DocNumber": "26-02-0042"}},  # no Id, no SyncToken
        )

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"
    assert "Id" in body["error"]["message"] or "SyncToken" in body["error"]["message"]


def test_append_line_invoice_with_no_existing_lines_works(settings_env, token_store):
    """An empty invoice (created via POST /invoices with no lines) must
    support the first append — Line key absent → treat as empty list.
    """
    empty_invoice = _invoice("42", SyncToken="0")  # no Line key

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": empty_invoice})
        body = json.loads(request.content)
        assert len(body["Line"]) == 1
        return _post_response(_invoice_for_append("42", sync_token="1"))

    client, _ = _make_client(token_store, handler)
    resp = client.post(
        "/v1/invoices/42/lines",
        json={"item_id": "11", "qty": 1, "rate": 75.0},
    )
    assert resp.status_code == 200


# ---------- PUT (full replace) endpoint ----------


def _operation(request: httpx.Request) -> str | None:
    """Pull the QBO `operation` query param off an outbound write request."""
    return parse_qs(urlparse(str(request.url)).query).get("operation", [None])[0]


def _minorversion(request: httpx.Request) -> str | None:
    """Pull the QBO `minorversion` query param off an outbound request. Every
    QBO call must pin minorversion=75 (project guideline); asserting it on the
    write paths keeps that contract from regressing silently."""
    return parse_qs(urlparse(str(request.url)).query).get("minorversion", [None])[0]


def test_put_replaces_invoice_and_returns_envelope(settings_env, token_store):
    """Acceptance: PUT replaces the invoice wholesale and returns the updated
    invoice in the DetailResponse envelope. The route reads the current
    invoice for its SyncToken, then full-updates.
    """
    replaced = _invoice("42", SyncToken="4", TotalAmt=150.0)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        assert request.method == "POST"
        return _post_response(replaced)

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0042",
            "lines": [{"item_id": "9", "qty": 2, "rate": 75.0}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["TotalAmt"] == 150.0


def test_put_sends_full_update_with_id_and_sync_token_no_sparse(settings_env, token_store):
    """The outbound body carries Id + SyncToken and the full Line array, and
    must NOT set sparse=true (this is a full replace, not a sparse update).
    The operation query param is also omitted for a plain update.
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"Invoice": _invoice_for_append("42", sync_token="7")}
            )
        body = json.loads(request.content)
        assert body["Id"] == "42"
        assert body["SyncToken"] == "7"
        assert body.get("sparse") is not True
        assert _operation(request) is None
        assert _minorversion(request) == "75"
        assert body["CustomerRef"] == {"value": "55"}
        assert body["DocNumber"] == "26-02-0042"
        assert len(body["Line"]) == 1
        line = body["Line"][0]
        assert line["DetailType"] == "SalesItemLineDetail"
        assert line["Amount"] == 150.0  # 2 * 75
        assert line["SalesItemLineDetail"]["ItemRef"] == {"value": "9"}
        return _post_response(_invoice("42", SyncToken="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0042",
            "lines": [{"item_id": "9", "qty": 2, "rate": 75.0}],
        },
    )
    assert resp.status_code == 200


def test_put_with_empty_lines_sends_explicit_empty_line_array(settings_env, token_store):
    """PUT is a full replace, not a merge: a body with no lines must send an
    explicit `Line: []` so QBO is told to clear the lines rather than
    (ambiguously) preserving the existing ones. This contrasts with create,
    which omits the Line key on empty input. If QBO rejects a zero-line
    invoice, its fault surfaces through the normal write error mapping.
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        assert _minorversion(request) == "75"
        body = json.loads(request.content)
        assert body["Line"] == []
        assert body["Id"] == "42"
        assert body["SyncToken"] == "3"
        assert body.get("sparse") is not True
        return _post_response(_invoice("42", SyncToken="4"))

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={"customer_id": "55", "doc_number": "26-02-0042"},  # no lines
    )
    assert resp.status_code == 200


def test_put_invalid_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/0;DROP",
        json={"customer_id": "55", "doc_number": "26-02-0042"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


def test_put_stale_sync_token_returns_409(settings_env, token_store):
    """Acceptance: stale SyncToken (QBO 5010) → 409 QBO_STALE_SYNC_TOKEN."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={"customer_id": "55", "doc_number": "26-02-0042"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]
    assert "Stale Object" in body["error"]["qbo_detail"]


def test_put_unknown_invoice_returns_404(settings_env, token_store):
    """Acceptance: not found → 404. The GET phase surfaces the missing id."""
    def handler(request):
        assert request.method == "GET"  # POST must not fire
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

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/9999",
        json={"customer_id": "55", "doc_number": "26-02-0042"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_put_unknown_body_field_returns_422(settings_env, token_store):
    """Acceptance: PUT body validation is strict (extra=forbid); unknown
    fields → 422.
    """
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0042",
            "tnx_date": "2026-02-15",  # typo for txn_date
        },
    )
    assert resp.status_code == 422


def test_put_line_without_rate_returns_422(settings_env, token_store):
    """Same as create: every line on a full replace must carry a rate."""
    def handler(request):
        pytest.fail("QBO must not be hit when a line is missing its rate")

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={
            "customer_id": "55",
            "doc_number": "26-02-0042",
            "lines": [{"item_id": "9", "qty": 1}],  # rate absent
        },
    )
    assert resp.status_code == 422


def test_put_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={"customer_id": "55", "doc_number": "26-02-0042"},
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_put_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.put(
        "/v1/invoices/42",
        json={"customer_id": "55", "doc_number": "26-02-0042"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ---------- DELETE endpoint ----------


def _delete_confirmation(id_: str) -> httpx.Response:
    """Shape QBO returns for operation=delete: an Invoice stub with a
    `status: Deleted` marker rather than the full record.
    """
    return httpx.Response(
        200,
        json={"Invoice": {"Id": id_, "status": "Deleted", "domain": "QBO"}},
    )


def test_delete_removes_invoice_and_returns_confirmation(settings_env, token_store):
    """Acceptance: DELETE deletes via operation=delete and returns the QBO
    confirmation in the standard envelope.
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        assert request.method == "POST"
        assert _operation(request) == "delete"
        assert _minorversion(request) == "75"
        body = json.loads(request.content)
        assert body == {"Id": "42", "SyncToken": "3"}
        return _delete_confirmation("42")

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["status"] == "Deleted"


def test_delete_invalid_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/0;DROP")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


def test_delete_already_deleted_passes_through_404(settings_env, token_store):
    """Acceptance: deleting an already-deleted invoice surfaces QBO's error
    (not masked). The GET phase reports object-not-found → 404.
    """
    def handler(request):
        assert request.method == "GET"  # POST must not fire
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

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/9999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_delete_stale_sync_token_returns_409(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]


def test_delete_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_delete_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ---------- void endpoint ----------


def test_void_voids_invoice_and_returns_record(settings_env, token_store):
    """Acceptance: POST /void voids via operation=void and returns the voided
    invoice (record survives at $0).
    """
    voided = _invoice("42", SyncToken="4", TotalAmt=0.0, PrivateNote="Voided")

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        assert request.method == "POST"
        assert _operation(request) == "void"
        assert _minorversion(request) == "75"
        body = json.loads(request.content)
        assert body == {"Id": "42", "SyncToken": "3"}
        return _post_response(voided)

    client, _ = _make_client(token_store, handler)
    resp = client.post("/v1/invoices/42/void")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["Id"] == "42"
    assert body["data"]["TotalAmt"] == 0.0


def test_void_invalid_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.post("/v1/invoices/0;DROP/void")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


def test_void_unknown_invoice_returns_404(settings_env, token_store):
    def handler(request):
        assert request.method == "GET"  # POST must not fire
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

    client, _ = _make_client(token_store, handler)
    resp = client.post("/v1/invoices/9999/void")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_void_stale_sync_token_returns_409(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.post("/v1/invoices/42/void")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]


def test_void_propagates_qbo_5xx_as_502(settings_env, token_store):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return httpx.Response(500, json={"Fault": {"Error": [{"Message": "boom"}]}})

    client, _ = _make_client(token_store, handler)
    resp = client.post("/v1/invoices/42/void")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "QBO_ERROR"


def test_void_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.post("/v1/invoices/42/void")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ---------- shared read-then-write helper edge cases ----------


def test_write_read_phase_missing_invoice_body_returns_502(settings_env, token_store):
    """A 200 from the read phase with no Invoice key is an upstream contract
    break, not a missing record (QBO signals not-found with a 400/404 fault).
    It must surface as 502 QBO_ERROR rather than a misleading 404, and the
    write POST must not fire. Exercised here via DELETE but shared by all four
    invoice writes through _load_invoice_for_write.
    """
    def handler(request):
        assert request.method == "GET"  # write POST must not fire
        return httpx.Response(200, json={"time": "2026-02-15T10:00:00Z"})  # no Invoice

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"


def test_write_read_phase_non_dict_invoice_body_returns_502(settings_env, token_store):
    """A 200 whose Invoice is a non-object (e.g. `{"Invoice": []}`) is an
    upstream contract break too. The shared read helper must return 502 rather
    than crash dereferencing a list, and the write POST must not fire.
    """
    def handler(request):
        assert request.method == "GET"  # write POST must not fire
        return httpx.Response(200, json={"Invoice": []})  # malformed, non-dict

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"


def test_write_phase_non_dict_invoice_body_returns_502(settings_env, token_store):
    """The write phase has the same guard as the read phase: a 200 whose
    Invoice is a non-object (e.g. `{"Invoice": []}`) must return 502 rather
    than crash building the DetailResponse. Exercised via DELETE: the GET
    succeeds, the delete POST returns the malformed body.
    """
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})
        return httpx.Response(200, json={"Invoice": []})  # malformed write response

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "QBO_ERROR"


# ---------- line update / delete endpoints (PUT/DELETE .../lines/{line_id}) ----------


def _invoice_two_lines(id_: str, sync_token: str = "3") -> dict:
    """An invoice with TWO item lines (Ids "1" and "2") plus the QBO-appended
    SubTotalLineDetail. Used so single-line edit/delete can prove the *other*
    line and the subtotal are left untouched.
    """
    return _invoice(
        id_,
        SyncToken=sync_token,
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
                "Id": "2",
                "LineNum": 2,
                "Amount": 100.0,
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "11", "name": "Nitrate Test"},
                    "Qty": 2,
                    "UnitPrice": 50.0,
                },
            },
            {
                "Amount": 175.0,
                "DetailType": "SubTotalLineDetail",
                "SubTotalLineDetail": {},
            },
        ],
    )


# ----- PUT .../lines/{line_id} -----


def test_put_line_replaces_matching_line_returns_updated_invoice(settings_env, token_store):
    """Acceptance: PUT replaces exactly the matching line; the route returns
    the updated invoice in the standard envelope (200)."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})
        assert request.method == "POST"
        return _post_response(_invoice_two_lines("42", sync_token="4"))

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/2",
        json={"item_id": "11", "qty": 4, "rate": 50.0},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["Id"] == "42"


def test_put_line_sends_sparse_update_replacing_only_target(settings_env, token_store):
    """The outbound sparse body keeps the untouched line verbatim, strips the
    SubTotal, and replaces the target line in place (Id preserved so QBO
    updates rather than re-numbers it)."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42", sync_token="7")})
        body = json.loads(request.content)
        assert body["Id"] == "42"
        assert body["SyncToken"] == "7"
        assert body["sparse"] is True
        # SubTotal stripped; both item lines remain.
        assert len(body["Line"]) == 2
        detail_types = [line["DetailType"] for line in body["Line"]]
        assert "SubTotalLineDetail" not in detail_types
        # Line "1" is untouched.
        line1 = next(line for line in body["Line"] if line["Id"] == "1")
        assert line1["SalesItemLineDetail"]["ItemRef"] == {"value": "9", "name": "Coliform Test"}
        assert line1["Amount"] == 75.0
        # Line "2" is replaced in place: same Id, new qty/rate/amount.
        line2 = next(line for line in body["Line"] if line["Id"] == "2")
        assert line2["Amount"] == 200.0  # 4 * 50
        assert line2["SalesItemLineDetail"]["ItemRef"] == {"value": "11"}
        assert line2["SalesItemLineDetail"]["Qty"] == 4
        assert line2["SalesItemLineDetail"]["UnitPrice"] == 50.0
        return _post_response(_invoice_two_lines("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/2",
        json={"item_id": "11", "qty": 4, "rate": 50.0},
    )
    assert resp.status_code == 200


def test_put_line_without_rate_omits_unit_price_and_amount(settings_env, token_store):
    """Same rate-less semantics as append: omit UnitPrice AND Amount so QBO
    falls back to the item's stored default price."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})
        body = json.loads(request.content)
        line2 = next(line for line in body["Line"] if line["Id"] == "2")
        assert "Amount" not in line2
        assert "UnitPrice" not in line2["SalesItemLineDetail"]
        return _post_response(_invoice_two_lines("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/2",
        json={"item_id": "11", "qty": 3},  # no rate
    )
    assert resp.status_code == 200


def test_put_line_unknown_line_returns_404(settings_env, token_store):
    """Acceptance: unknown line_id (invoice exists, line doesn't) → 404 with a
    line-specific message. The write (POST) must not fire."""
    def handler(request):
        assert request.method == "GET"  # no sparse POST for a missing line
        return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/999",
        json={"item_id": "11", "qty": 1, "rate": 50.0},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "999" in body["error"]["message"]
    assert "42" in body["error"]["message"]


def test_put_line_unknown_invoice_returns_404(settings_env, token_store):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            400,
            json={
                "Fault": {
                    "Error": [
                        {"Message": "Object Not Found", "Detail": "Object Not Found", "code": "610"}
                    ],
                    "type": "ValidationFault",
                }
            },
        )

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/9999/lines/1",
        json={"item_id": "11", "qty": 1, "rate": 50.0},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "9999" in body["error"]["message"]


def test_put_line_stale_sync_token_returns_409(settings_env, token_store):
    """Acceptance: stale SyncToken → 409 QBO_STALE_SYNC_TOKEN."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/2",
        json={"item_id": "11", "qty": 1, "rate": 50.0},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]
    assert "Stale Object" in body["error"]["qbo_detail"]


def test_put_line_invalid_invoice_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/0;DROP/lines/1",
        json={"item_id": "11", "qty": 1, "rate": 50.0},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


def test_put_line_invalid_line_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed line_id")

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/abc",
        json={"item_id": "11", "qty": 1, "rate": 50.0},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "line_id" in body["error"]["message"].lower()


def test_put_line_missing_required_field_returns_422(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.put("/v1/invoices/42/lines/2", json={"item_id": "11"})  # qty missing
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_put_line_unknown_body_field_returns_422(settings_env, token_store):
    """Acceptance: PUT body is strict (extra=forbid); unknown fields → 422."""
    def handler(request):
        pytest.fail("QBO must not be hit when the request body fails validation")

    client, _ = _make_client(token_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/2",
        json={"item_id": "11", "qty": 1, "rate": 50.0, "unit_price": 50.0},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_put_line_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.put(
        "/v1/invoices/42/lines/2",
        json={"item_id": "11", "qty": 1, "rate": 50.0},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"


# ----- DELETE .../lines/{line_id} -----


def test_delete_line_removes_matching_line_returns_updated_invoice(settings_env, token_store):
    """Acceptance: DELETE removes exactly the matching line; route returns the
    updated invoice (200)."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})
        assert request.method == "POST"
        return _post_response(_invoice_two_lines("42", sync_token="4"))

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42/lines/2")
    assert resp.status_code == 200
    assert resp.json()["data"]["Id"] == "42"


def test_delete_line_sends_sparse_update_without_target(settings_env, token_store):
    """The outbound body keeps the surviving line, strips the SubTotal, and
    omits the deleted line entirely."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42", sync_token="7")})
        body = json.loads(request.content)
        assert body["Id"] == "42"
        assert body["SyncToken"] == "7"
        assert body["sparse"] is True
        # Only line "1" survives; SubTotal stripped; line "2" gone.
        assert len(body["Line"]) == 1
        assert body["Line"][0]["Id"] == "1"
        ids = [line.get("Id") for line in body["Line"]]
        assert "2" not in ids
        detail_types = [line["DetailType"] for line in body["Line"]]
        assert "SubTotalLineDetail" not in detail_types
        return _post_response(_invoice_two_lines("42", sync_token="8"))

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42/lines/2")
    assert resp.status_code == 200


def test_delete_last_line_returns_409_not_502(settings_env, token_store):
    """Acceptance: deleting the last remaining line returns a clear 4xx, not a
    QBO 502 pass-through. Pin the exact 409 + CANNOT_DELETE_LAST_LINE contract
    so a regression off it can't slip through a loose status range. The write
    (POST) must not fire — we catch it before QBO would reject the empty Line
    array."""
    def handler(request):
        assert request.method == "GET"  # must short-circuit before the POST
        return httpx.Response(200, json={"Invoice": _invoice_for_append("42")})

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42/lines/1")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "CANNOT_DELETE_LAST_LINE"
    assert "1" in body["error"]["message"]


def test_delete_line_unknown_line_returns_404(settings_env, token_store):
    """Acceptance: unknown line_id on DELETE → 404. The write must not fire."""
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42/lines/999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "999" in body["error"]["message"]
    assert "42" in body["error"]["message"]


def test_delete_line_unknown_invoice_returns_404(settings_env, token_store):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            400,
            json={
                "Fault": {
                    "Error": [
                        {"Message": "Object Not Found", "Detail": "Object Not Found", "code": "610"}
                    ],
                    "type": "ValidationFault",
                }
            },
        )

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/9999/lines/1")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_delete_line_stale_sync_token_returns_409(settings_env, token_store):
    """Acceptance: stale SyncToken on the write-back → 409."""
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"Invoice": _invoice_two_lines("42")})
        return _stale_sync_token_response()

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42/lines/2")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "QBO_STALE_SYNC_TOKEN"
    assert "42" in body["error"]["message"]
    assert "Stale Object" in body["error"]["qbo_detail"]


def test_delete_line_invalid_invoice_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed invoice_id")

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/0;DROP/lines/1")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "invoice_id" in body["error"]["message"].lower()


def test_delete_line_invalid_line_id_rejected_without_hitting_qbo(settings_env, token_store):
    def handler(request):
        pytest.fail("QBO must not be hit for malformed line_id")

    client, _ = _make_client(token_store, handler)
    resp = client.delete("/v1/invoices/42/lines/abc")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_PARAM"
    assert "line_id" in body["error"]["message"].lower()


def test_delete_line_unauthenticated_returns_503(settings_env, tmp_path):
    empty_store = FileTokenStore(path=tmp_path / "missing-tokens.json")

    def handler(request):
        pytest.fail("QBO should never be hit without auth")

    client, _ = _make_client(empty_store, handler)
    resp = client.delete("/v1/invoices/42/lines/1")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "NOT_AUTHENTICATED"
