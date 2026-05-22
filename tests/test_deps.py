from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from qbsvc.auth.secret_manager import SecretManagerTokenStore
from qbsvc.auth.tokens import FileTokenStore
from qbsvc.config import Settings
from qbsvc.deps import get_token_store, reset_token_store_cache
from qbsvc.main import create_app


@pytest.fixture(autouse=True)
def _clear_token_store_cache():
    reset_token_store_cache()
    yield
    reset_token_store_cache()


def test_file_backend_is_default():
    settings = Settings(token_backend="file")
    store = get_token_store(settings=settings)
    assert isinstance(store, FileTokenStore)


def test_secret_manager_backend_selected_via_env(monkeypatch):
    fake_client = object()
    with patch(
        "qbsvc.deps._build_secret_manager_client", return_value=fake_client
    ) as build:
        settings = Settings(
            token_backend="secret_manager",
            gcp_project="mwl-prod",
            secret_name_tokens="mwl-qb-tokens",
        )
        store = get_token_store(settings=settings)
    assert isinstance(store, SecretManagerTokenStore)
    build.assert_called_once()


def test_secret_manager_backend_is_memoized():
    """One instance per process so the threading.Lock actually serializes
    across two FastAPI dependency injections of the same store.
    """
    with patch("qbsvc.deps._build_secret_manager_client", return_value=object()):
        settings = Settings(
            token_backend="secret_manager",
            gcp_project="mwl-prod",
            secret_name_tokens="mwl-qb-tokens",
        )
        first = get_token_store(settings=settings)
        second = get_token_store(settings=settings)
    assert first is second
    # Explicit: the lock is shared too. Regression-guards a future refactor
    # that returns fresh store instances and silently breaks serialization.
    assert first.refresh_lock is second.refresh_lock


def test_secret_manager_backend_requires_gcp_project():
    settings = Settings(token_backend="secret_manager", gcp_project="")
    with pytest.raises(ValueError, match="QBSVC_GCP_PROJECT"):
        get_token_store(settings=settings)


def test_healthz_still_returns_200_with_secret_manager_backend(monkeypatch):
    monkeypatch.setenv("QBSVC_TOKEN_BACKEND", "secret_manager")
    monkeypatch.setenv("QBSVC_GCP_PROJECT", "mwl-prod")
    # Reset the cached Settings since env changed.
    from qbsvc.config import get_settings

    get_settings.cache_clear()
    with patch("qbsvc.deps._build_secret_manager_client", return_value=object()):
        client = TestClient(create_app())
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_still_returns_200_with_file_backend():
    client = TestClient(create_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
