"""Startup warm-up of the Secret Manager token path.

On a cold Cloud Run instance the first request used to pay for importing the
Secret Manager client, opening its channel and loading (often refreshing) the
QBO token — 12-47s in prod, well past callers' timeouts. Doing it in the app
lifespan moves that cost before uvicorn binds the port, so Cloud Run's startup
probe holds traffic until the token path is warm.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from qbsvc.config import get_settings
from qbsvc.deps import reset_token_store_cache
from qbsvc.main import create_app


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("QBSVC_TOKEN_BACKEND", "secret_manager")
    monkeypatch.setenv("QBSVC_GCP_PROJECT", "mwl-prod")
    monkeypatch.setenv("QBSVC_SECRET_NAME_TOKENS", "mwl-qb-tokens")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QBSVC_INTUIT_CLIENT_SECRET", "test-secret")
    get_settings.cache_clear()
    reset_token_store_cache()
    yield
    get_settings.cache_clear()
    reset_token_store_cache()


def _client_with_fresh_token() -> MagicMock:
    client = MagicMock()
    resp = MagicMock()
    resp.payload.data = json.dumps(
        {
            "access_token": "at",
            "refresh_token": "rt",
            "realm_id": "realm-1",
            "expires_at": time.time() + 3600,
        }
    ).encode("utf-8")
    client.access_secret_version.return_value = resp
    return client


def test_startup_loads_the_token_before_serving():
    sm = _client_with_fresh_token()
    with patch("qbsvc.deps._build_secret_manager_client", return_value=sm) as build:
        with TestClient(create_app()):
            # Lifespan startup has run; no request has been made yet.
            build.assert_called_once()
            sm.access_secret_version.assert_called_once()


def test_startup_warmup_failure_does_not_block_startup():
    sm = MagicMock()
    sm.access_secret_version.side_effect = RuntimeError("secret manager down")
    with patch("qbsvc.deps._build_secret_manager_client", return_value=sm):
        with TestClient(create_app()) as client:
            assert client.get("/healthz").status_code == 200


def test_startup_skips_warmup_for_file_backend(monkeypatch):
    monkeypatch.setenv("QBSVC_TOKEN_BACKEND", "file")
    get_settings.cache_clear()
    with patch("qbsvc.deps.get_token_store") as get_store:
        with TestClient(create_app()):
            get_store.assert_not_called()
