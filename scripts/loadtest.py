"""Fire unique transaction webhooks at the service and report throughput and latency.

Usage: python scripts/loadtest.py <base_url> [total_requests] [concurrency]
Example: python scripts/loadtest.py http://localhost:8000 3000 20

The client is plain Python, so at high concurrency it can become the bottleneck
itself; use a C tool such as `ab` to measure the server's ceiling.
"""

import asyncio
import statistics
import sys
import time
import uuid
from collections import Counter

import httpx


async def run(base_url: str, total: int, concurrency: int) -> None:
    url = f"{base_url.rstrip('/')}/v1/webhooks/transactions"
    pending = [f"txn_load_{uuid.uuid4().hex[:16]}" for _ in range(total)]
    latencies: list[float] = []
    outcomes: Counter = Counter()

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:

        async def sender() -> None:
            while pending:
                transaction_id = pending.pop()
                body = {
                    "transaction_id": transaction_id,
                    "source_account": "acc_user_789",
                    "destination_account": "acc_merchant_456",
                    "amount": 1500,
                    "currency": "INR",
                }
                started = time.perf_counter()
                try:
                    response = await client.post(url, json=body)
                    outcomes[response.status_code] += 1
                except httpx.HTTPError as exc:
                    outcomes[type(exc).__name__] += 1
                latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(sender() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started

    cuts = statistics.quantiles(latencies, n=100)
    print(
        f"{total} requests, concurrency {concurrency}: {total / elapsed:.0f} req/s\n"
        f"latency p50 {cuts[49]:.0f} ms, p95 {cuts[94]:.0f} ms, p99 {cuts[98]:.0f} ms, "
        f"max {max(latencies):.0f} ms\n"
        f"responses: {dict(outcomes)}"
    )


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    total_requests = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    asyncio.run(run(base, total_requests, workers))
