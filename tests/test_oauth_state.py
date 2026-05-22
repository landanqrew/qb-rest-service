from __future__ import annotations

from qbsvc.auth.oauth_state import OAuthStateStore


class _Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_create_returns_unique_state_tokens():
    store = OAuthStateStore()
    a = store.create()
    b = store.create()
    assert a != b
    assert len(a) >= 20


def test_consume_valid_state_returns_true():
    store = OAuthStateStore()
    state = store.create()
    assert store.consume(state) is True


def test_consume_unknown_state_returns_false():
    store = OAuthStateStore()
    assert store.consume("not-a-real-state") is False


def test_consume_is_one_time():
    store = OAuthStateStore()
    state = store.create()
    assert store.consume(state) is True
    assert store.consume(state) is False


def test_consume_expired_state_returns_false():
    clock = _Clock(t=1000.0)
    store = OAuthStateStore(ttl_seconds=60, clock=clock)
    state = store.create()
    clock.t = 1061.0  # just past TTL
    assert store.consume(state) is False


def test_consume_within_ttl_returns_true():
    clock = _Clock(t=1000.0)
    store = OAuthStateStore(ttl_seconds=60, clock=clock)
    state = store.create()
    clock.t = 1059.0  # within TTL
    assert store.consume(state) is True


def test_create_purges_expired_entries():
    clock = _Clock(t=0.0)
    store = OAuthStateStore(ttl_seconds=10, clock=clock)
    stale = store.create()
    clock.t = 100.0
    # Creating a new entry should purge the stale one so it can't be consumed
    # even within its own (now-expired) window.
    store.create()
    assert store.consume(stale) is False
    # Internal dict should only hold the fresh entry.
    assert len(store._states) == 1
