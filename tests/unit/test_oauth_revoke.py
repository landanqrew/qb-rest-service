from __future__ import annotations

import httpx
import pytest

from qbsvc.auth import oauth as oauth_mod
from qbsvc.config import Settings
from qbsvc.exceptions import AuthError


def _settings() -> Settings:
    return Settings(
        intuit_client_id="test-client-id",
        intuit_client_secret="test-secret",
    )


def _capture_post(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, *, auth=None, json=None, data=None, **kwargs):
        calls.append({"url": url, "auth": auth, "json": json, "data": data})
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req)

    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", fake_post)
    return calls


def test_revoke_posts_token_with_basic_auth_to_revocation_endpoint(monkeypatch):
    calls = _capture_post(monkeypatch)

    oauth_mod.revoke(_settings(), "the-refresh-token")

    assert len(calls) == 1
    assert "revoke" in calls[0]["url"]
    assert calls[0]["auth"] == ("test-client-id", "test-secret")
    # Intuit's revoke endpoint takes the token in a JSON body.
    assert calls[0]["json"] == {"token": "the-refresh-token"}


def test_revoke_raises_auth_error_on_non_200(monkeypatch):
    def failing_post(url, *, auth=None, json=None, **kwargs):
        req = httpx.Request("POST", url)
        return httpx.Response(400, text="invalid_token", request=req)

    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", failing_post)

    with pytest.raises(AuthError, match="revoke failed"):
        oauth_mod.revoke(_settings(), "dead-token")


def test_revoke_requires_credentials(monkeypatch):
    monkeypatch.setattr(
        "qbsvc.auth.oauth.httpx.post",
        lambda *a, **k: pytest.fail("revoke should not call Intuit without creds"),
    )
    with pytest.raises(AuthError, match="Missing Intuit credentials"):
        oauth_mod.revoke(Settings(), "some-token")
