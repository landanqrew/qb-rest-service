from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import httpx
import pytest

from qbsvc.api.client import QBClient
from qbsvc.api.rate_limit import TokenBucket
from qbsvc.auth.tokens import FileTokenStore, TokenData
from qbsvc.config import Settings
from qbsvc.exceptions import RateLimitError


# ---------- TokenBucket unit tests ----------


def test_bucket_starts_full():
    bucket = TokenBucket(rate_per_sec=1.0, capacity=5, clock=lambda: 0.0)
    for _ in range(5):
        assert bucket.try_acquire() is True


def test_bucket_refuses_when_drained():
    bucket = TokenBucket(rate_per_sec=1.0, capacity=3, clock=lambda: 0.0)
    for _ in range(3):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_bucket_refills_at_rate():
    """At 2 tokens/sec, after 1 second elapsed the bucket gains 2 tokens."""
    now = [0.0]
    bucket = TokenBucket(rate_per_sec=2.0, capacity=4, clock=lambda: now[0])

    # Drain
    for _ in range(4):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False

    # Advance 1s -> +2 tokens available
    now[0] = 1.0
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_bucket_refill_caps_at_capacity():
    """Long idle period cannot grow the bucket past capacity."""
    now = [0.0]
    bucket = TokenBucket(rate_per_sec=1.0, capacity=3, clock=lambda: now[0])

    # Drain one
    assert bucket.try_acquire() is True

    # Skip ahead a huge amount of time; bucket should only have `capacity`
    # tokens, not capacity + 999.
    now[0] = 1000.0
    for _ in range(3):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_bucket_steady_state_at_limit_succeeds():
    """Acceptance: steady-state at the configured rate succeeds without 429s.

    Refill at rate r; if a caller takes one token every 1/r seconds it should
    never be denied.
    """
    now = [0.0]
    bucket = TokenBucket(rate_per_sec=8.0, capacity=8, clock=lambda: now[0])

    # Drain to zero so refill timing is the only thing keeping us afloat
    for _ in range(8):
        assert bucket.try_acquire() is True

    # 20 acquisitions, each 1/8s apart — at the limit, must never refuse
    for _ in range(20):
        now[0] += 1.0 / 8.0
        assert bucket.try_acquire() is True, "steady-state at the limit should not 429"


def test_bucket_is_thread_safe():
    """Concurrent callers must between them acquire exactly `capacity` tokens
    in a small window where no refill has occurred (advancing clock is held
    constant for the test). Race conditions would let multiple threads see
    the same token and over-acquire.
    """
    bucket = TokenBucket(rate_per_sec=0.0, capacity=50, clock=lambda: 0.0)

    successes: list[bool] = []
    lock = threading.Lock()

    def grab() -> None:
        ok = bucket.try_acquire()
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=grab) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(successes) == 50


# ---------- QBClient integration ----------


@pytest.fixture
def fresh_store(tmp_path: Path) -> FileTokenStore:
    store = FileTokenStore(path=tmp_path / "tokens.json")
    store.save(
        TokenData(
            access_token="t",
            refresh_token="r",
            realm_id="realm-Z",
            expires_at=time.time() + 3600,
        )
    )
    return store


def _client(transport: httpx.MockTransport, store: FileTokenStore, **kw) -> QBClient:
    c = QBClient(token_store=store, settings=Settings(), **kw)
    c._http.close()
    c._http = httpx.Client(transport=transport, timeout=30)
    return c


def test_qbclient_raises_local_rate_limit_when_bucket_drained(fresh_store):
    """Acceptance: burst beyond the bucket returns 429 immediately — no HTTP
    call to QBO is made when the local bucket refuses.
    """
    bucket = TokenBucket(rate_per_sec=0.0, capacity=2, clock=lambda: 0.0)

    upstream_hits = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        upstream_hits["n"] += 1
        return httpx.Response(200, json={"Customer": {"Id": "1"}})

    client = _client(httpx.MockTransport(handler), fresh_store, rate_limiter=bucket)

    # First 2 succeed (bucket capacity = 2)
    client.get("customer/1")
    client.get("customer/1")
    assert upstream_hits["n"] == 2

    # Third must be refused locally with retry_after set, and must not hit QBO
    with pytest.raises(RateLimitError) as excinfo:
        client.get("customer/1")
    assert excinfo.value.retry_after == 1
    assert upstream_hits["n"] == 2, "local rate-limit must not reach QBO"


def test_qbclient_logs_when_local_rate_limit_triggered(caplog, fresh_store):
    """Acceptance: rate-limit denial is logged so operators can see it."""
    caplog.set_level(logging.WARNING, logger="qbsvc.qbo")

    bucket = TokenBucket(rate_per_sec=0.0, capacity=0, clock=lambda: 0.0)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler), fresh_store, rate_limiter=bucket)

    with pytest.raises(RateLimitError):
        client.get("customer/1")

    msgs = [r for r in caplog.records if r.name == "qbsvc.qbo" and r.levelno >= logging.WARNING]
    assert msgs, "local rate-limit must emit a warning log line"
    rec = msgs[-1]
    assert rec.__dict__.get("qbo_endpoint") == "customer/1"


def test_qbclient_unbounded_by_default(fresh_store):
    """Default QBClient (no injected limiter) must still allow plenty of
    sequential requests — the limiter only kicks in past the configured rate.
    """
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Customer": {}})

    settings = Settings()  # defaults: 480/min, burst 16
    c = QBClient(token_store=fresh_store, settings=settings)
    c._http.close()
    c._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)

    # 5 sequential calls (well under burst) must all succeed
    for _ in range(5):
        c.get("customer/1")


# ---------- Settings ----------


def test_settings_have_rate_limit_defaults():
    s = Settings()
    assert s.rate_limit_per_min == 480
    assert s.rate_limit_burst == 16


def test_settings_pick_up_env_overrides(monkeypatch):
    monkeypatch.setenv("QBSVC_RATE_LIMIT_PER_MIN", "120")
    monkeypatch.setenv("QBSVC_RATE_LIMIT_BURST", "4")
    s = Settings()
    assert s.rate_limit_per_min == 120
    assert s.rate_limit_burst == 4


# ---------- Error handler integration ----------


def test_rate_limit_handler_sets_retry_after_header():
    """When RateLimitError carries retry_after, the handler must emit a
    Retry-After header (per acceptance: 429 + Retry-After: 1).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from qbsvc.errors import register_exception_handlers
    from qbsvc.middleware import RequestIDMiddleware

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/burst")
    def _burst():
        raise RateLimitError("Local rate limit exceeded.", retry_after=1)

    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/burst")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "1"
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_handler_omits_header_when_no_retry_after():
    """The QBO-after-retry path raises RateLimitError without retry_after;
    that response must still be a clean 429 with no spurious Retry-After header.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from qbsvc.errors import register_exception_handlers
    from qbsvc.middleware import RequestIDMiddleware

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/qbo-rate")
    def _qbo_rate():
        raise RateLimitError("Rate limited after retry. Try again later.")

    c = TestClient(app, raise_server_exceptions=False)
    resp = c.get("/qbo-rate")
    assert resp.status_code == 429
    assert "Retry-After" not in resp.headers
