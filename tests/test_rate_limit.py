from app.main import app
from app.ratelimit import TokenBucketLimiter
from tests.helpers import WEBHOOK_URL, make_payload


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_bucket_allows_burst_then_refuses():
    limiter = TokenBucketLimiter(rate=1, burst=3, clock=FakeClock())

    results = [limiter.allow("1.2.3.4")[0] for _ in range(4)]

    assert results == [True, True, True, False]


def test_bucket_refills_over_time():
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=2, burst=1, clock=clock)
    limiter.allow("1.2.3.4")

    allowed, retry_after = limiter.allow("1.2.3.4")
    assert not allowed
    assert retry_after == 0.5

    clock.now += 0.5
    assert limiter.allow("1.2.3.4")[0]


def test_clients_have_separate_buckets():
    limiter = TokenBucketLimiter(rate=1, burst=1, clock=FakeClock())

    assert limiter.allow("1.1.1.1")[0]
    assert limiter.allow("2.2.2.2")[0]
    assert not limiter.allow("1.1.1.1")[0]


def test_idle_clients_are_forgotten_when_table_is_full():
    clock = FakeClock()
    limiter = TokenBucketLimiter(rate=1, burst=1, max_clients=2, clock=clock)
    limiter.allow("old-1")
    limiter.allow("old-2")

    clock.now += 5
    limiter.allow("new")

    assert set(limiter._buckets) == {"new"}


def test_api_returns_429_with_retry_after(client, monkeypatch):
    limiter = TokenBucketLimiter(rate=0.5, burst=2, clock=FakeClock())
    monkeypatch.setattr(app.state, "rate_limiter", limiter)

    codes = [
        client.post(WEBHOOK_URL, json=make_payload(f"txn_rl_{i}")).status_code for i in range(3)
    ]

    assert codes == [202, 202, 429]
    refused = client.get("/v1/transactions/txn_rl_0")
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "2"


def test_health_check_is_never_rate_limited(client, monkeypatch):
    monkeypatch.setattr(app.state, "rate_limiter", TokenBucketLimiter(rate=0.01, burst=1))

    assert all(client.get("/").status_code == 200 for _ in range(5))
