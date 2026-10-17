"""End-to-end check of a running deployment, using only the standard library.

Usage: python scripts/smoke_test.py <base_url> [seconds_to_wait_for_processing]
Example: python scripts/smoke_test.py https://your-service.onrender.com

Walks through the assignment's success criteria: health and readiness, a webhook
answered with 202 inside 500 ms, duplicates (sequential and simultaneous)
ignored, the transaction PROCESSED after the delay, and 404/422 handling.
Exits non-zero if any check fails.
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
WAIT_SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 35
FAILURES = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw.decode(errors="replace")
    return status, payload, elapsed_ms


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        FAILURES.append(name)


def webhook(transaction_id, **overrides):
    body = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    body.update(overrides)
    return call("POST", "/v1/webhooks/transactions", body)


def main():
    print(f"Smoke testing {BASE}\n")
    transaction_id = f"txn_smoke_{uuid.uuid4().hex[:12]}"

    status, body, ms = call("GET", "/")
    check("GET / is HEALTHY", status == 200 and body.get("status") == "HEALTHY", f"{ms:.0f} ms")

    status, body, ms = call("GET", "/ready")
    check("GET /ready is READY", status == 200 and body.get("status") == "READY", f"{ms:.0f} ms")

    status, body, ms = webhook(transaction_id)
    check(
        "webhook accepted with 202",
        status == 202 and body.get("duplicate") is False,
        f"{status}, {ms:.0f} ms",
    )
    check("webhook answered within 500 ms", ms < 500, f"{ms:.0f} ms")

    status, body, ms = webhook(transaction_id)
    check(
        "repeat webhook is 202 and flagged duplicate",
        status == 202 and body.get("duplicate") is True,
        f"{ms:.0f} ms",
    )

    race_id = f"txn_smoke_race_{uuid.uuid4().hex[:12]}"
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: webhook(race_id), range(10)))
    firsts = sum(1 for s, b, _ in results if s == 202 and b.get("duplicate") is False)
    check(
        "10 simultaneous duplicates -> exactly one new transaction",
        all(s == 202 for s, _, _ in results) and firsts == 1,
        f"{firsts} accepted as new",
    )

    status, body, _ = call("GET", f"/v1/transactions/{transaction_id}")
    check(
        "lookup returns PROCESSING straight away",
        status == 200 and isinstance(body, list) and body[0]["status"] == "PROCESSING",
        body[0]["status"] if status == 200 else str(status),
    )

    status, _, _ = call("GET", "/v1/transactions/txn_smoke_does_not_exist")
    check("unknown transaction is 404", status == 404, str(status))

    status, _, _ = webhook(f"txn_smoke_bad_{uuid.uuid4().hex[:8]}", amount=-5)
    check("invalid amount is 422", status == 422, str(status))

    print(f"\nWaiting {WAIT_SECONDS:.0f}s for background processing...")
    time.sleep(WAIT_SECONDS)

    status, body, _ = call("GET", f"/v1/transactions/{transaction_id}")
    transaction = body[0] if status == 200 else {}
    check(
        "transaction is PROCESSED after the delay",
        transaction.get("status") == "PROCESSED" and transaction.get("processed_at"),
        f"created {transaction.get('created_at')}, processed {transaction.get('processed_at')}",
    )

    print(f"\n{'All checks passed' if not FAILURES else f'{len(FAILURES)} check(s) failed'}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
