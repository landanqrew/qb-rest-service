"""Browser-safe OAuth bootstrap flow (issue #51).

Verifies the acceptance criteria for a user-facing "Connect QuickBooks" button:

- The flow can be started from a plain browser (no Cloud Run identity token),
  authorized by a signed *launch token* minted by the consuming app.
- Non-admin traffic (no/invalid launch token) cannot initiate the flow.
- On success the browser is redirected back to the consuming app's return URL.
- The data API can be turned off on the public bootstrap deployment so its
  surface is admin OAuth only.
- Re-auth/token rotation runs through the same browser-safe path and writes the
  existing token store.

These exercise the real ``create_app()`` wiring via ``TestClient``.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.auth import admin_launch
from qbsvc.auth.oauth_state import OAuthStateStore
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import get_settings
from qbsvc.deps import (
    get_oauth_state_store,
    get_token_store,
    reset_oauth_state_store_cache,
    reset_token_store_cache,
)
from qbsvc.main import create_app

REDIRECT_URI = "https://qb-admin.example.com/admin/oauth/callback"
LAUNCH_SECRET = "shared-launch-secret"
RETURN_URL = "https://sample-manager.example.com/settings/integrations"


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
def state_store():
    return OAuthStateStore(ttl_seconds=600)


@pytest.fixture
def token_store(tmp_path: Path):
    return FileTokenStore(path=tmp_path / "tokens.json")


def _make_client(monkeypatch, state_store, token_store, **env):
    base = {
        "QBSVC_INTUIT_CLIENT_ID": "test-client-id",
        "QBSVC_INTUIT_CLIENT_SECRET": "test-secret",
        "QBSVC_OAUTH_REDIRECT_URI": REDIRECT_URI,
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_oauth_state_store] = lambda: state_store
    app.dependency_overrides[get_token_store] = lambda: token_store
    return TestClient(app, follow_redirects=False)


def _launch() -> str:
    return admin_launch.mint_launch_token(LAUNCH_SECRET, ttl_seconds=300)


def _mock_token_response(monkeypatch):
    def fake_post(url, *, auth=None, data=None, **kwargs):
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


# --------------------------------------------------------------------------- #
# Launch gate on /admin/oauth/start
# --------------------------------------------------------------------------- #


def test_start_with_valid_launch_token_redirects_to_intuit(
    monkeypatch, state_store, token_store
):
    """A browser click carrying a valid launch token reaches Intuit consent —
    no Cloud Run identity token needed."""
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET
    )
    resp = client.get("/admin/oauth/start", params={"launch": _launch()})
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith(
        "https://appcenter.intuit.com/connect/oauth2"
    )


def test_start_without_launch_token_is_forbidden(monkeypatch, state_store, token_store):
    """Non-admin browser traffic (no launch token) cannot initiate the flow."""
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET
    )
    resp = client.get("/admin/oauth/start")
    assert resp.status_code == 403
    # Must not have minted a state token for an unauthorized initiation.
    # (No location header / no redirect.)
    assert "location" not in {k.lower() for k in resp.headers}


def test_start_with_invalid_launch_token_is_forbidden(
    monkeypatch, state_store, token_store
):
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET
    )
    resp = client.get("/admin/oauth/start", params={"launch": "bogus.token"})
    assert resp.status_code == 403


def test_start_with_expired_launch_token_is_forbidden(
    monkeypatch, state_store, token_store
):
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET
    )
    expired = admin_launch.mint_launch_token(LAUNCH_SECRET, ttl_seconds=1, now=1.0)
    resp = client.get("/admin/oauth/start", params={"launch": expired})
    assert resp.status_code == 403


def test_start_needs_no_token_when_launch_gate_disabled(
    monkeypatch, state_store, token_store
):
    """With no launch secret configured, behavior is unchanged: /start works
    without a token (local dev / IAM-fronted deployment)."""
    client = _make_client(monkeypatch, state_store, token_store)
    resp = client.get("/admin/oauth/start")
    assert resp.status_code in (302, 307)


def test_start_forwarder_jwt_still_allowed_with_launch_gate(
    monkeypatch, state_store, token_store
):
    """The operator's identity-injecting forwarder path (an allowlisted Cloud
    Run identity token) keeps working even when the launch gate is enabled."""
    import base64
    import json

    def jwt(email: str) -> str:
        def b64(d):
            return base64.urlsafe_b64encode(
                json.dumps(d).encode()
            ).rstrip(b"=").decode()

        return f"{b64({'alg': 'RS256'})}.{b64({'email': email})}.sig"

    client = _make_client(
        monkeypatch,
        state_store,
        token_store,
        QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET,
        QBSVC_ADMIN_ALLOWLIST="ops@example.com",
    )
    resp = client.get(
        "/admin/oauth/start",
        headers={"Authorization": f"Bearer {jwt('ops@example.com')}"},
    )
    assert resp.status_code in (302, 307)


# --------------------------------------------------------------------------- #
# Callback: redirect back to the consuming app
# --------------------------------------------------------------------------- #


def test_callback_redirects_to_return_url_on_success(
    monkeypatch, state_store, token_store
):
    client = _make_client(
        monkeypatch,
        state_store,
        token_store,
        QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET,
        QBSVC_ADMIN_RETURN_URL=RETURN_URL,
    )
    _mock_token_response(monkeypatch)
    state = state_store.create()

    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "auth-code", "realmId": "9341454312345678", "state": state},
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith(RETURN_URL)
    q = parse_qs(urlparse(loc).query)
    assert q["qb_connected"] == ["1"]
    assert q["realmId"] == ["9341454312345678"]
    # Token still persisted through the existing store.
    saved = token_store.load()
    assert saved is not None and saved.refresh_token == "new-refresh"


def test_callback_without_return_url_keeps_success_page(
    monkeypatch, state_store, token_store
):
    """When no return URL is configured, the standalone success page (current
    behavior) is preserved."""
    client = _make_client(monkeypatch, state_store, token_store)
    _mock_token_response(monkeypatch)
    state = state_store.create()
    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "1", "state": state},
    )
    assert resp.status_code == 200
    assert "successful" in resp.text.lower()


def test_callback_reauth_rotates_token_via_browser_flow(
    monkeypatch, state_store, token_store
):
    """Re-auth through the same browser-safe path overwrites the stored token."""
    token_store.save(
        TokenData(
            access_token="old", refresh_token="old-r", realm_id="old", expires_at=0.0
        )
    )
    client = _make_client(
        monkeypatch,
        state_store,
        token_store,
        QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET,
        QBSVC_ADMIN_RETURN_URL=RETURN_URL,
    )
    _mock_token_response(monkeypatch)
    state = state_store.create()
    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "new-realm", "state": state},
    )
    assert resp.status_code == 303
    reloaded = token_store.load()
    assert reloaded is not None
    assert reloaded.refresh_token == "new-refresh"
    assert reloaded.realm_id == "new-realm"


def test_callback_state_mismatch_still_blocks_completion(
    monkeypatch, state_store, token_store
):
    """A browser without a valid state (i.e. one that never went through the
    gated /start) cannot complete the bootstrap."""
    client = _make_client(
        monkeypatch,
        state_store,
        token_store,
        QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET,
        QBSVC_ADMIN_RETURN_URL=RETURN_URL,
    )
    monkeypatch.setattr(
        "qbsvc.auth.oauth.httpx.post",
        lambda *a, **k: pytest.fail("token exchange must not run on bad state"),
    )
    resp = client.get(
        "/admin/oauth/callback",
        params={"code": "x", "realmId": "1", "state": "never-issued"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# disconnect is gated by the launch token too
# --------------------------------------------------------------------------- #


def test_disconnect_get_forbidden_without_launch_token(
    monkeypatch, state_store, token_store
):
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET
    )
    resp = client.get("/admin/oauth/disconnect")
    assert resp.status_code == 403


def test_disconnect_get_allowed_with_launch_token(
    monkeypatch, state_store, token_store
):
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET
    )
    resp = client.get("/admin/oauth/disconnect", params={"launch": _launch()})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Route-group toggles (deploy the same image as data-only or admin-only)
# --------------------------------------------------------------------------- #


def test_data_routes_absent_when_disabled(monkeypatch, state_store, token_store):
    """On the public bootstrap deployment the data API is turned off entirely,
    so its surface can never be reached there."""
    client = _make_client(
        monkeypatch,
        state_store,
        token_store,
        QBSVC_ENABLE_DATA_ROUTES="false",
        QBSVC_ADMIN_LAUNCH_SECRET=LAUNCH_SECRET,
    )
    assert client.get("/v1/customers").status_code == 404
    # Admin bootstrap still reachable.
    assert client.get(
        "/admin/oauth/start", params={"launch": _launch()}
    ).status_code in (302, 307)


def test_admin_routes_absent_when_disabled(monkeypatch, state_store, token_store):
    """On the IAM-locked data deployment the admin OAuth surface can be turned
    off so it isn't hosted there at all."""
    client = _make_client(
        monkeypatch, state_store, token_store, QBSVC_ENABLE_ADMIN_ROUTES="false"
    )
    assert client.get("/admin/oauth/start").status_code == 404
    # Data + health still reachable.
    assert client.get("/healthz").status_code == 200


def test_healthz_always_present(monkeypatch, state_store, token_store):
    client = _make_client(
        monkeypatch,
        state_store,
        token_store,
        QBSVC_ENABLE_DATA_ROUTES="false",
        QBSVC_ENABLE_ADMIN_ROUTES="false",
    )
    assert client.get("/healthz").status_code == 200
