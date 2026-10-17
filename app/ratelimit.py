import time
from collections.abc import Callable


class TokenBucketLimiter:
    """Per-client token bucket, kept in memory.

    Each client gets `burst` tokens that refill at `rate` per second; a request
    spends one token. No locking is needed: allow() never awaits, so on a single
    event loop it can't be interleaved with another call.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.rate = rate
        self.burst = burst
        self.max_clients = max_clients
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, seconds until the next token is available)."""
        now = self._clock()
        tokens, last = self._buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)

        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            allowed, retry_after = True, 0.0
        else:
            self._buckets[key] = (tokens, now)
            allowed, retry_after = False, (1 - tokens) / self.rate

        if len(self._buckets) > self.max_clients:
            self._forget_idle_clients(now)
        return allowed, retry_after

    def _forget_idle_clients(self, now: float) -> None:
        # A client idle long enough to be back at a full bucket loses nothing by being dropped.
        refill_time = self.burst / self.rate
        self._buckets = {
            key: state for key, state in self._buckets.items() if now - state[1] < refill_time
        }
