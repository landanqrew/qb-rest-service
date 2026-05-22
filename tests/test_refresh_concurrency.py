"""Concurrent-refresh serialization (issue #16).

These tests cover the production wiring for `TokenStore.refresh_lock`:
two FastAPI requests that both arrive with an expired access token must
result in exactly one call to Intuit's token endpoint, and the loser must
adopt the rotated token persisted by the winner — never call refresh a
second time with a now-invalidated refresh token.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from qbsvc.api.client import QBClient
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings, get_settings
from qbsvc.deps import get_token_store, reset_token_store_cache
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
def shared_store(tmp_path: Path) -> FileTokenStore:
    store = FileTokenStore(path=tmp_path / "tokens.json")
    store.save(
        TokenData(
            access_token="stale-access",
            refresh_token="stale-refresh",
            realm_id="realm-123",
            expires_at=time.time() - 3600,
        )
    )
    return store


class _RefreshRecorder:
    """Mock for Intuit's token endpoint that widens the refresh race window
    and counts how many times it was hit.
    """

    def __init__(self, *, latency_s: float = 0.1):
        self.latency_s = latency_s
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._counter = 0

    def __call__(self, url, *, auth=None, data=None, **kwargs):
        with self._lock:
            self._counter += 1
            n = self._counter
            self.calls.append(url)
        # Sleep OUTSIDE the lock so concurrent callers actually overlap
        # inside the endpoint — without this, a fast mock would serialize
        # naturally and we couldn't observe whether the production lock
        # is doing the serialization.
        time.sleep(self.latency_s)
        req = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "access_token": f"new-access-{n}",
                "refresh_token": f"new-refresh-{n}",
                "expires_in": 3600,
            },
            request=req,
        )


def test_two_concurrent_qbclient_refreshes_hit_intuit_once(
    settings_env, shared_store, monkeypatch
):
    """Acceptance criterion: two threads that both trigger _refresh_tokens
    against the same store call Intuit exactly once. Without the lock, both
    would race and the loser would invalidate the winner's refresh token.
    """
    recorder = _RefreshRecorder(latency_s=0.1)
    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", recorder)

    settings = Settings()
    # Barrier ensures both threads reach _refresh_tokens simultaneously
    # before either acquires the lock, so the test deterministically
    # exercises the race — without it, a fast scheduler could let thread
    # 1 finish before thread 2 even starts, masking a broken lock.
    barrier = threading.Barrier(2)

    def worker() -> TokenData:
        client = QBClient(token_store=shared_store, settings=settings)
        try:
            # Prime each client with the expired token so both decide to
            # refresh, exactly as two fresh per-request QBClient instances
            # would in production.
            client._tokens = shared_store.load()
            barrier.wait(timeout=5)
            return client._refresh_tokens()
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(worker)
        f2 = ex.submit(worker)
        t1 = f1.result(timeout=5)
        t2 = f2.result(timeout=5)

    assert len(recorder.calls) == 1, (
        f"expected exactly one Intuit refresh, got {len(recorder.calls)}"
    )
    # Both workers end up with the SAME rotated token — the loser reloaded
    # it from the store instead of calling refresh a second time.
    assert t1.refresh_token == t2.refresh_token
    assert t1.access_token == t2.access_token
    persisted = shared_store.load()
    assert persisted is not None
    assert persisted.access_token == t1.access_token


def test_two_concurrent_readyz_requests_hit_intuit_once(
    settings_env, shared_store, monkeypatch
):
    """End-to-end acceptance: two concurrent /readyz calls through FastAPI
    must produce exactly one Intuit refresh. This is the integration test
    the issue's acceptance criteria call out specifically.
    """
    recorder = _RefreshRecorder(latency_s=0.1)
    monkeypatch.setattr("qbsvc.auth.oauth.httpx.post", recorder)

    app = create_app()
    app.dependency_overrides[get_token_store] = lambda: shared_store
    client = TestClient(app)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(client.get, "/readyz")
        f2 = ex.submit(client.get, "/readyz")
        r1 = f1.result(timeout=5)
        r2 = f2.result(timeout=5)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert len(recorder.calls) == 1, (
        f"expected exactly one Intuit refresh across two requests, "
        f"got {len(recorder.calls)}"
    )


def test_refresh_lock_is_held_during_oauth_call(
    settings_env, shared_store, monkeypatch
):
    """White-box: the lock must be held while oauth.refresh is in flight,
    not just briefly grabbed and released before the network call. Otherwise
    the lock-then-refresh sequence still races.
    """
    settings = Settings()
    lock_held_during_refresh: list[bool] = []

    def fake_post(url, *, auth=None, data=None, **kwargs):
        lock_held_during_refresh.append(
            shared_store.refresh_lock.locked()  # threading.Lock API
        )
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

    qbclient = QBClient(token_store=shared_store, settings=settings)
    try:
        qbclient._tokens = shared_store.load()
        qbclient._refresh_tokens()
    finally:
        qbclient.close()

    assert lock_held_during_refresh == [True], (
        "refresh_lock was not held while oauth.refresh was in flight"
    )
