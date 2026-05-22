from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """Process-local token bucket for outbound QBO calls.

    Refills continuously at `rate_per_sec` tokens/sec up to `capacity`.
    `try_acquire()` returns True and consumes a token if one is available,
    else returns False so the caller can fail fast instead of waiting.

    Sized off QBO's documented 500 req/min/realm limit: deployments default
    to 480/min refill with a 16-token burst so we trip well before Intuit
    does.

    The clock is injectable so unit tests can drive refill behavior
    deterministically without sleeping.
    """

    def __init__(
        self,
        rate_per_sec: float,
        capacity: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 0:
            raise ValueError("capacity must be >= 0")
        if rate_per_sec < 0:
            raise ValueError("rate_per_sec must be >= 0")
        self._rate = float(rate_per_sec)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._last_refill = clock()
        self._lock = threading.Lock()

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._last_refill)
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False
