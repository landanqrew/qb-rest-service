from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.auth.oauth_state import OAuthStateStore
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings, get_settings
from qbsvc.deps import (
    get_oauth_state_store,
    get_token_store,
    reset_oauth_state_store_cache,
    reset_token_store_cache,
)
from qbsvc.main import create_app


REDIRECT_URI = "https://qb-service.example.com/admin/oauth/callback"


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    reset_token_store_cache()
    reset_oauth_state_store_cache()
    yield
    get_settings.cache_clear()
    reset_token_store_cache()
    reset_oauth_state_store_cache()


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("QBSVC_OAUTH_REDIRECT_URI", REDIRECT_URI)


@pytest.fixture
def state_store():
    return OAuthStateStore(ttl_seconds=600)


@pytest.fixture
def token_store(tmp_path: Path):
    return FileTokenStore(path=tmp_path / "tokens.json")


@pytest.fixture
def client(settings_env, state_store, token_store):
    app = create_app()
    app.dependency_overrides[get_oauth_state_store] = lambda: state_store
    app.dependency_overrides[get_token_store] = lambda: token_store
    return TestClient(app, follow_redirects=False)


# ---------- /admin/oauth/start ----------


def test_start_redirects_to_intuit(client):
    resp = client.get("/admin/oauth/start")
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert location.startswith("https://appcenter.intuit.com/connect/oauth2")


def test_start_includes_required_query_params(client):
    resp = client.get("/admin/oauth/start")
    parsed = urlparse(resp.headers["location"])
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["test-client-id"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == ["com.intuit.quickbooks.accounting"]
    assert len(params["state"][0]) >= 20


def test_start_state_is_stored_for_callback_validation(client, state_store):
    resp = client.get("/admin/oauth/start")
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    # The state we just minted should validate on consume.
    assert state_store.consume(state) is True


def test_start_returns_500_when_oauth_not_configured(monkeypatch, state_store):
    # No client_id / no redirect_uri set.
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_oauth_state_store] = lambda: state_store
    c = TestClient(app, follow_redirects=False)
    resp = c.get("/admin/oauth/start")
    assert resp.status_code == 500


# ---------- /admin/oauth/callback ----------


def _mock_token_response(monkeypatch):
    """Patch httpx.post so the token exchange returns a fixed payload."""
    calls = []

    def fake_post(url, *, auth=None, data=None, **kwargs):
        calls.append({"url": url, "auth": auth, "data": data})
        req = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
            request=req,
        )

    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", fake_post)
    return calls


def test_callback_valid_state_exchanges_and_saves_tokens(
    client, state_store, token_store, monkeypatch
):
    state = state_store.create()
    calls = _mock_token_response(monkeypatch)

    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "auth-code-xyz", "realmId": "9341454312345678", "state": state},
    )

    assert resp.status_code == 200
    assert "successful" in resp.text.lower()
    saved = token_store.load()
    assert saved is not None
    assert saved.access_token == "new-access"
    assert saved.refresh_token == "new-refresh"
    assert saved.realm_id == "9341454312345678"
    # The redirect_uri sent to Intuit must match the authorize step exactly.
    assert calls[0]["data"]["redirect_uri"] == REDIRECT_URI
    assert calls[0]["data"]["code"] == "auth-code-xyz"
    assert calls[0]["auth"] == ("test-client-id", "test-secret")


def test_callback_state_mismatch_returns_400(client, monkeypatch):
    # Don't create any state — anything we pass is bogus.
    monkeypatch.setattr(
        "qbsvc.auth.oauth.httpx.post",
        lambda *a, **k: pytest.fail("token exchange should not be called"),
    )
    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "1", "state": "bogus"},
    )
    assert resp.status_code == 400
    assert "state" in resp.text.lower()


def test_callback_consumes_state_one_time(client, state_store, token_store, monkeypatch):
    state = state_store.create()
    _mock_token_response(monkeypatch)

    first = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "1", "state": state},
    )
    assert first.status_code == 200

    second = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "1", "state": state},
    )
    assert second.status_code == 400


def test_callback_intuit_error_returns_400(client, state_store, monkeypatch):
    state = state_store.create()
    monkeypatch.setattr(
        "qbsvc.auth.oauth.httpx.post",
        lambda *a, **k: pytest.fail("token exchange should not be called on error"),
    )
    resp = client.get(
        "/admin/oauth/callback",
        params={"state": state, "error": "access_denied"},
    )
    assert resp.status_code == 400
    assert "access_denied" in resp.text


def test_callback_missing_code_returns_400(client, state_store):
    state = state_store.create()
    resp = client.get(
        "/admin/oauth/callback",
        params={"state": state, "realmId": "1"},
    )
    assert resp.status_code == 400


def test_callback_missing_realm_id_returns_400(client, state_store):
    state = state_store.create()
    resp = client.get(
        "/admin/oauth/callback",
        params={"state": state, "code": "x"},
    )
    assert resp.status_code == 400


def test_callback_reauth_overwrites_existing_tokens(
    client, state_store, token_store, monkeypatch
):
    # Pre-existing tokens — first auth round.
    token_store.save(
        TokenData(
            access_token="old-access",
            refresh_token="old-refresh",
            realm_id="old-realm",
            expires_at=0.0,
        )
    )
    state = state_store.create()
    _mock_token_response(monkeypatch)

    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "new-realm", "state": state},
    )
    assert resp.status_code == 200

    reloaded = token_store.load()
    assert reloaded is not None
    assert reloaded.access_token == "new-access"
    assert reloaded.refresh_token == "new-refresh"
    assert reloaded.realm_id == "new-realm"


def test_callback_token_exchange_failure_returns_502(
    client, state_store, token_store, monkeypatch
):
    state = state_store.create()

    def failing_post(url, *, auth=None, data=None, **kwargs):
        req = httpx.Request("POST", url)
        return httpx.Response(400, text="invalid_grant", request=req)

    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", failing_post)
    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "1", "state": state},
    )
    assert resp.status_code == 502
    # Token store untouched.
    assert token_store.load() is None


def test_state_store_dependency_is_memoized():
    """One state store per process so /start and /callback share entries
    across requests."""
    settings = Settings(
        intuit_client_id="x",
        intuit_client_secret="y",
        oauth_redirect_uri=REDIRECT_URI,
    )
    a = get_oauth_state_store(settings=settings)
    b = get_oauth_state_store(settings=settings)
    assert a is b
