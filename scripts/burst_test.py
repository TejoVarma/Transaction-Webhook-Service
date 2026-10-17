"""Fire N webhooks at the same instant and report how many were accepted.

Usage: python scripts/burst_test.py <base_url> <count> [label]

Every request gets a unique transaction_id (prefixed with the label), so the
number of rows stored afterwards can be checked against the number accepted.
"""

import asyncio
import sys
import time
from collections import Counter

import httpx


async def burst(base_url: str, count: int, label: str) -> None:
    url = f"{base_url.rstrip('/')}/v1/webhooks/transactions"
    gate = asyncio.Event()
    limits = httpx.Limits(max_connections=count, max_keepalive_connections=0)

    async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(120)) as client:

        async def send(index: int):
            body = {
                "transaction_id": f"txn_burst_{label}_{index}",
                "source_account": "acc_user_789",
                "destination_account": "acc_merchant_456",
                "amount": 1500,
                "currency": "INR",
            }
            await gate.wait()
            try:
                response = await client.post(url, json=body)
                return response.status_code
            except httpx.HTTPError as exc:
                return type(exc).__name__

        tasks = [asyncio.create_task(send(i)) for i in range(count)]
        await asyncio.sleep(0.5)  # let every task reach the gate first
        started = time.perf_counter()
        gate.set()
        results = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

    outcome = Counter(results)
    accepted = outcome.get(202, 0)
    print(
        f"burst of {count}: accepted {accepted}/{count} ({accepted / count:.0%}) "
        f"in {elapsed:.1f}s | {dict(outcome)}"
    )


if __name__ == "__main__":
    base = sys.argv[1]
    total = int(sys.argv[2])
    run_label = sys.argv[3] if len(sys.argv) > 3 else str(total)
    asyncio.run(burst(base, total, run_label))
