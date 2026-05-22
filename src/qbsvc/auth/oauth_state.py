from __future__ import annotations

import secrets
import threading
import time
from typing import Callable


class OAuthStateStore:
    """In-memory CSRF state tokens for the Intuit OAuth handshake.

    Single-process; loses contents on restart. Acceptable here because a
    state token's lifetime is one browser round-trip — the only cost of a
    restart mid-flow is that the operator has to click "Connect" again.
    """

    def __init__(
        self,
        ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._states: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        state = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            self._states[state] = now
        return state

    def consume(self, state: str) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            issued = self._states.pop(state, None)
        if issued is None:
            return False
        return (now - issued) <= self.ttl_seconds

    def _purge_expired_locked(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        expired = [s for s, t in self._states.items() if t < cutoff]
        for s in expired:
            del self._states[s]
