from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings


@pytest.fixture
def primed_client(tmp_path: Path):
    store = FileTokenStore(path=tmp_path / "tokens.json")
    store.save(
        TokenData(
            access_token="fresh-access",
            refresh_token="fresh-refresh",
            realm_id="realm-xyz",
            expires_at=time.time() + 3600,
        )
    )
    client = QBClient(token_store=store, settings=Settings())

    def _attach(handler):
        client._http.close()
        client._http = httpx.Client(
            transport=httpx.MockTransport(handler), timeout=30
        )
        return client

    yield _attach
    client.close()


def test_query_returns_entity_named_by_sql(primed_client):
    """The route surfaces only the rows for the entity named in `FROM <Entity>`,
    not whatever list QBO happens to put first in QueryResponse.
    """

    def handler(request):
        return httpx.Response(
            200,
            json={
                "QueryResponse": {
                    # Imagine QBO ever surfaced a sibling list (warnings, etc.)
                    # before the entity list — the implementation must still
                    # return the entity rows, not the foreign list.
                    "Warnings": [{"Message": "irrelevant"}],
                    "Customer": [{"Id": "1"}, {"Id": "2"}],
                    "maxResults": 2,
                }
            },
        )

    client = primed_client(handler)
    rows = client.query("SELECT * FROM Customer WHERE Active = true")
    assert rows == [{"Id": "1"}, {"Id": "2"}]


def test_query_returns_empty_list_when_entity_absent(primed_client):
    """If QBO returns QueryResponse without the entity key (e.g. zero matches),
    the result is a clean empty list — never None, never a sibling list.
    """

    def handler(request):
        return httpx.Response(200, json={"QueryResponse": {"maxResults": 0}})

    client = primed_client(handler)
    rows = client.query("SELECT * FROM Customer WHERE Active = true")
    assert rows == []


def test_query_entity_lookup_is_case_insensitive(primed_client):
    """QBO response keys are PascalCase; the SQL `FROM` clause should not
    have to match that exact casing for the lookup to find the rows.
    Guards against a subtle silent-empty bug in future routes.
    """

    def handler(request):
        return httpx.Response(
            200,
            json={"QueryResponse": {"Customer": [{"Id": "1"}], "maxResults": 1}},
        )

    client = primed_client(handler)
    rows = client.query("select * from customer where Active = true")
    assert rows == [{"Id": "1"}]
