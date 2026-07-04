"""Tests for the /admin/* identity allowlist middleware.

Issue #13: enforce admin/caller separation at the application layer because
Cloud Run IAM is service-level, not path-level. The middleware reads the
caller's email from the Authorization JWT (already verified by Cloud Run at
the edge) and 403s any /admin/* request whose email is not on the allowlist.

When the allowlist is empty (`QBSVC_ADMIN_ALLOWLIST` unset), the gate is OFF
so local dev keeps working without a JWT.
"""
from __future__ import annotations

import base64
import json
from typing import Iterable

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from qbsvc.auth.admin_gate import (
    AdminGateMiddleware,
    ensure_admin_gate_configured,
    extract_email_from_bearer,
)
from qbsvc.config import Settings, get_settings
from qbsvc.middleware import RequestIDMiddleware


def _jwt(payload: dict) -> str:
    """Build a fake JWT (header.payload.signature). Signature is not verified
    by the middleware — Cloud Run validates it at the edge."""
    header = {"alg": "RS256", "typ": "JWT"}

    def b64(d: dict) -> str:
        raw = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}.signature-stub"


def _bearer(email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_jwt({'email': email})}"}


def _build_app(allowlist: Iterable[str]) -> FastAPI:
    """App with the admin gate + a couple of stub routes for assertions."""
    app = FastAPI()
    settings = Settings(admin_allowlist=list(allowlist))
    app.add_middleware(AdminGateMiddleware, settings_provider=lambda: settings)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/admin/oauth/start")
    def _start():
        return JSONResponse({"ok": True})

    @app.get("/admin/oauth/callback")
    def _callback():
        return JSONResponse({"ok": True})

    @app.get("/healthz")
    def _healthz():
        return {"status": "ok"}

    @app.get("/v1/customers")
    def _customers():
        return {"data": []}

    return app


# ---------- gate disabled (no allowlist configured) ----------


def test_gate_off_admin_open_when_allowlist_empty():
    """No allowlist configured → gate is OFF. Matches local-dev behavior where
    Cloud Run IAM doesn't exist."""
    client = TestClient(_build_app(allowlist=[]))
    resp = client.get("/admin/oauth/start")
    assert resp.status_code == 200


def test_gate_off_data_routes_open_when_allowlist_empty():
    client = TestClient(_build_app(allowlist=[]))
    resp = client.get("/v1/customers")
    assert resp.status_code == 200


# ---------- gate enabled ----------


ADMIN_EMAIL = "admin@example.com"
WEB_APP_SA = "web-app-runtime@web-project.iam.gserviceaccount.com"


def test_admin_identity_allowed_on_admin_paths():
    """AC: admin identity gets 200 on /admin/*."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/admin/oauth/start", headers=_bearer(ADMIN_EMAIL))
    assert resp.status_code == 200


def test_admin_identity_allowed_on_data_paths():
    """AC: admin identity gets 200 on data routes as well — the gate only
    inspects /admin/*, it never blocks data routes."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/v1/customers", headers=_bearer(ADMIN_EMAIL))
    assert resp.status_code == 200


def test_web_app_service_account_forbidden_on_admin_paths():
    """AC: web app service account gets 403 on /admin/*."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/admin/oauth/start", headers=_bearer(WEB_APP_SA))
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "ADMIN_FORBIDDEN"


def test_web_app_service_account_allowed_on_data_paths():
    """AC implicit: data routes remain reachable by the web app SA."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/v1/customers", headers=_bearer(WEB_APP_SA))
    assert resp.status_code == 200


def test_admin_paths_403_when_no_bearer_token_and_gate_enabled():
    """If the allowlist is configured, an /admin/* request without an
    Authorization header cannot be identified → deny."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/admin/oauth/start")
    assert resp.status_code == 403


def test_admin_paths_403_on_malformed_jwt():
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get(
        "/admin/oauth/start",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 403


def test_admin_paths_403_on_jwt_with_no_email_claim():
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    token = _jwt({"sub": "1234567890"})  # no email
    resp = client.get(
        "/admin/oauth/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_admin_paths_403_response_carries_request_id_header():
    """403s flow back through RequestIDMiddleware so callers can correlate
    the rejection in logs."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get(
        "/admin/oauth/start",
        headers={
            "Authorization": f"Bearer {_jwt({'email': WEB_APP_SA})}",
            "X-Request-ID": "test-rid-123",
        },
    )
    assert resp.status_code == 403
    assert resp.headers.get("x-request-id") == "test-rid-123"
    assert resp.json()["error"].get("request_id") == "test-rid-123"


def test_healthz_never_gated():
    """/healthz must never require auth — it's the Cloud Run startup probe."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_admin_callback_path_also_gated():
    """Both /admin/oauth/start and /admin/oauth/callback are admin surfaces;
    the gate matches any /admin/ prefix."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get(
        "/admin/oauth/callback",
        headers=_bearer(WEB_APP_SA),
    )
    assert resp.status_code == 403


def test_allowlist_is_case_insensitive_for_email():
    """Email comparison is case-insensitive; Google emits lowercase emails
    but the allowlist might be configured with mixed case."""
    client = TestClient(_build_app(allowlist=["Admin@Example.com"]))
    resp = client.get(
        "/admin/oauth/start",
        headers=_bearer("admin@example.com"),
    )
    assert resp.status_code == 200


def test_multiple_allowlist_entries_supported():
    """A second admin (e.g. an alternate Google identity) can be added."""
    client = TestClient(
        _build_app(allowlist=[ADMIN_EMAIL, "secondary-admin@example.com"])
    )
    resp = client.get(
        "/admin/oauth/start",
        headers=_bearer("secondary-admin@example.com"),
    )
    assert resp.status_code == 200


def test_data_paths_never_gated_even_with_no_auth_when_gate_enabled():
    """The gate only narrows /admin/*. Cloud Run IAM still owns the rest of
    the surface; the middleware does not double-up by adding application-layer
    auth to data routes."""
    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get("/v1/customers")  # no Authorization header
    assert resp.status_code == 200


# ---------- helper unit tests ----------


def test_extract_email_returns_lowercase():
    token = _jwt({"email": "Admin@Example.COM"})
    assert extract_email_from_bearer(f"Bearer {token}") == "admin@example.com"


def test_extract_email_returns_none_for_missing_header():
    assert extract_email_from_bearer(None) is None


def test_extract_email_returns_none_for_wrong_scheme():
    assert extract_email_from_bearer("Basic xyz") is None


def test_extract_email_returns_none_for_garbled_token():
    assert extract_email_from_bearer("Bearer not.a.jwt") is None


def test_extract_email_returns_none_when_payload_has_no_email():
    token = _jwt({"sub": "1234"})
    assert extract_email_from_bearer(f"Bearer {token}") is None


def test_extract_email_returns_none_for_non_dict_payload():
    """JWT payloads are JSON objects per spec, but a crafted token whose
    payload is a JSON array (or scalar) used to crash `.get(...)`. The
    guard turns it into a clean None → caller gets a 403, not a 500."""
    header_b64 = (
        base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    )
    array_payload_b64 = (
        base64.urlsafe_b64encode(b"[1,2,3]").rstrip(b"=").decode()
    )
    token = f"{header_b64}.{array_payload_b64}.sig"
    assert extract_email_from_bearer(f"Bearer {token}") is None


def test_admin_paths_403_on_jwt_with_array_payload():
    """End-to-end: a forged token with an array payload returns 403, not 500."""
    header_b64 = (
        base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    )
    array_payload_b64 = (
        base64.urlsafe_b64encode(b"[1,2,3]").rstrip(b"=").decode()
    )
    token = f"{header_b64}.{array_payload_b64}.sig"

    client = TestClient(_build_app(allowlist=[ADMIN_EMAIL]))
    resp = client.get(
        "/admin/oauth/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---------- settings parsing ----------


def test_admin_allowlist_parses_from_comma_separated_env(monkeypatch):
    """`QBSVC_ADMIN_ALLOWLIST=a@x,b@y` → ['a@x', 'b@y']. Pydantic-settings
    is the source of truth; this exercise pins the contract so a future
    refactor doesn't silently break env parsing."""
    monkeypatch.setenv("QBSVC_ADMIN_ALLOWLIST", "a@example.com,b@example.com")
    get_settings.cache_clear()
    s = get_settings()
    assert s.admin_allowlist == ["a@example.com", "b@example.com"]
    get_settings.cache_clear()


def test_admin_allowlist_trims_whitespace_and_drops_empties(monkeypatch):
    monkeypatch.setenv(
        "QBSVC_ADMIN_ALLOWLIST", " a@example.com , , b@example.com "
    )
    get_settings.cache_clear()
    s = get_settings()
    assert s.admin_allowlist == ["a@example.com", "b@example.com"]
    get_settings.cache_clear()


def test_admin_allowlist_empty_by_default(monkeypatch):
    monkeypatch.delenv("QBSVC_ADMIN_ALLOWLIST", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.admin_allowlist == []
    get_settings.cache_clear()


# ---------- integration with the real /admin/oauth/start route ----------


def test_real_admin_oauth_start_blocked_for_non_admin(monkeypatch):
    """End-to-end through `create_app()`: the real /admin/oauth/start router
    must sit behind the gate. Prevents a future refactor from accidentally
    registering an admin router outside the middleware stack."""
    monkeypatch.setenv("QBSVC_ADMIN_ALLOWLIST", ADMIN_EMAIL)
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_ID", "x")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_SECRET", "y")
    monkeypatch.setenv(
        "QBSVC_OAUTH_REDIRECT_URI",
        "https://qb-service.example.com/admin/oauth/callback",
    )
    get_settings.cache_clear()

    from qbsvc.main import create_app

    client = TestClient(create_app(), follow_redirects=False)
    resp = client.get("/admin/oauth/start", headers=_bearer(WEB_APP_SA))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ADMIN_FORBIDDEN"

    get_settings.cache_clear()


def test_real_admin_oauth_start_allowed_for_admin(monkeypatch):
    monkeypatch.setenv("QBSVC_ADMIN_ALLOWLIST", ADMIN_EMAIL)
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_ID", "x")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_SECRET", "y")
    monkeypatch.setenv(
        "QBSVC_OAUTH_REDIRECT_URI",
        "https://qb-service.example.com/admin/oauth/callback",
    )
    get_settings.cache_clear()

    from qbsvc.main import create_app

    client = TestClient(create_app(), follow_redirects=False)
    resp = client.get("/admin/oauth/start", headers=_bearer(ADMIN_EMAIL))
    # Real route redirects to Intuit when allowed through.
    assert resp.status_code in (302, 307)

    get_settings.cache_clear()


# ---------- startup guard: gate cannot be silently OFF on Cloud Run ----------


def test_startup_guard_raises_on_cloud_run_without_allowlist(monkeypatch):
    """Cloud Run sets K_SERVICE; an empty allowlist there means /admin/* is open
    to any roles/run.invoker caller. The app must refuse to start."""
    monkeypatch.setenv("K_SERVICE", "qb-service")
    with pytest.raises(RuntimeError, match="no gate configured"):
        ensure_admin_gate_configured(Settings(admin_allowlist=[]))


def test_startup_guard_passes_on_cloud_run_with_allowlist(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "qb-service")
    # Does not raise.
    ensure_admin_gate_configured(Settings(admin_allowlist=[ADMIN_EMAIL]))


def test_startup_guard_noop_off_cloud_run(monkeypatch):
    """Local dev and the §3a OAuth bootstrap run without K_SERVICE and
    intentionally with an empty allowlist — the guard must stay silent."""
    monkeypatch.delenv("K_SERVICE", raising=False)
    ensure_admin_gate_configured(Settings(admin_allowlist=[]))
