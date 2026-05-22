from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings, get_settings
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
    return FileTokenStore(path=tmp_path / "tokens.json")


def _make_client(token_store) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_token_store] = lambda: token_store
    return TestClient(app)


def _seed_expired_token(store: FileTokenStore) -> None:
    store.save(
        TokenData(
            access_token="stale-access",
            refresh_token="stale-refresh",
            realm_id="realm-123",
            expires_at=time.time() - 60,
        )
    )


def _seed_fresh_token(store: FileTokenStore) -> None:
    store.save(
        TokenData(
            access_token="fresh-access",
            refresh_token="fresh-refresh",
            realm_id="realm-123",
            expires_at=time.time() + 3600,
        )
    )


def _patch_token_refresh(monkeypatch, *, success: bool = True):
    """Patch Intuit's token endpoint. Asserts only the token URL is hit."""
    calls = []

    def fake_post(url, *, auth=None, data=None, **kwargs):
        calls.append(url)
        req = httpx.Request("POST", url)
        if success:
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
                request=req,
            )
        return httpx.Response(400, text="invalid_grant", request=req)

    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", fake_post)
    return calls


def test_readyz_returns_503_when_no_token_present(settings_env, token_store):
    client = _make_client(token_store)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "NOT_AUTHENTICATED"
    assert "oauth" in body["error"]["message"].lower()


def test_readyz_returns_200_when_token_is_fresh(settings_env, token_store, monkeypatch):
    _seed_fresh_token(token_store)
    # No refresh should be triggered — fail loudly if it is.
    monkeypatch.setattr(
        "qbsvc.auth.oauth.httpx.post",
        lambda *a, **k: pytest.fail("refresh should not be called when token is fresh"),
    )
    client = _make_client(token_store)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_refreshes_expired_token_and_returns_200(
    settings_env, token_store, monkeypatch
):
    _seed_expired_token(token_store)
    calls = _patch_token_refresh(monkeypatch, success=True)
    client = _make_client(token_store)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(calls) == 1
    assert "oauth.platform.intuit.com" in calls[0]
    # Rotated refresh token must have been persisted.
    reloaded = token_store.load()
    assert reloaded is not None
    assert reloaded.access_token == "new-access"
    assert reloaded.refresh_token == "new-refresh"


def test_readyz_returns_503_when_refresh_fails(settings_env, token_store, monkeypatch):
    _seed_expired_token(token_store)
    _patch_token_refresh(monkeypatch, success=False)
    client = _make_client(token_store)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "TOKEN_REFRESH_FAILED"
    assert "refresh" in body["error"]["message"].lower()


def test_readyz_does_not_make_qbo_entity_request(
    settings_env, token_store, monkeypatch
):
    """Acceptance: /readyz exercises token refresh only, never a QBO API call."""
    _seed_expired_token(token_store)
    calls = _patch_token_refresh(monkeypatch, success=True)

    original_request = httpx.Client.request

    def guarded_request(self, method, url, *args, **kwargs):
        if "quickbooks.api.intuit.com" in str(url):
            pytest.fail(f"unexpected QBO entity request: {method} {url}")
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", guarded_request)

    client = _make_client(token_store)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    # Only Intuit's token endpoint was hit.
    assert all("oauth.platform.intuit.com" in c for c in calls)


def test_readyz_dependency_uses_get_qb_client():
    """Regression guard: the route must go through the QBClient dependency
    so production swaps (e.g. tests overriding get_qb_client) take effect.
    """
    app = create_app()

    sentinel_called = {"value": False}

    class FakeClient:
        def ensure_ready(self) -> None:
            sentinel_called["value"] = True

        def close(self) -> None:
            pass

    def fake_dep():
        yield FakeClient()

    app.dependency_overrides[get_qb_client] = fake_dep
    c = TestClient(app)
    resp = c.get("/readyz")
    assert resp.status_code == 200
    assert sentinel_called["value"] is True
