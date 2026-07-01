from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings


@pytest.fixture
def make_client(tmp_path: Path):
    """Build a QBClient with explicit settings and a MockTransport that
    records the request URL."""
    clients = []

    def _make(settings: Settings):
        store = FileTokenStore(path=tmp_path / "tokens.json")
        store.save(
            TokenData(
                access_token="fresh-access",
                refresh_token="fresh-refresh",
                realm_id="realm-xyz",
                expires_at=time.time() + 3600,
            )
        )
        client = QBClient(token_store=store, settings=settings)
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
        clients.append(client)
        return client, seen

    yield _make
    for c in clients:
        c.close()


def test_production_environment_uses_production_host(make_client):
    client, seen = make_client(Settings(intuit_environment="production"))
    client.get("customer/123")
    assert seen["url"].startswith(
        "https://quickbooks.api.intuit.com/v3/company/realm-xyz/customer/123"
    )


def test_sandbox_environment_uses_sandbox_host(make_client):
    """QBSVC_INTUIT_ENVIRONMENT=sandbox must route entity calls to the
    sandbox API host — sandbox realms 401 on the production host."""
    client, seen = make_client(Settings(intuit_environment="sandbox"))
    client.get("customer/123")
    assert seen["url"].startswith(
        "https://sandbox-quickbooks.api.intuit.com/v3/company/realm-xyz/customer/123"
    )
