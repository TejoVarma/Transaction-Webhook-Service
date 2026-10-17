import time

import pytest
from sqlalchemy import text

from app.db import engine

WEBHOOK_URL = "/v1/webhooks/transactions"


def make_payload(transaction_id="txn_test_1", **overrides):
    payload = {
        "transaction_id": transaction_id,
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    payload.update(overrides)
    return payload


async def fetch_row(transaction_id):
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM transactions WHERE transaction_id = :id"),
            {"id": transaction_id},
        )
        return result.mappings().first()


async def count_rows():
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM transactions"))).scalar_one()


async def insert_abandoned(transaction_id, locked_for_seconds):
    async with engine.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO transactions
                    (transaction_id, source_account, destination_account, amount, currency,
                     attempts, locked_until)
                VALUES (:id, 'acc_a', 'acc_b', 10, 'INR', 1,
                        now() + make_interval(secs => :locked_for))
            """),
            {"id": transaction_id, "locked_for": locked_for_seconds},
        )


def wait_for_status(client, transaction_id, expected, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/v1/transactions/{transaction_id}").json()
        if body[0]["status"] == expected:
            return body[0]
        time.sleep(0.25)
    pytest.fail(f"{transaction_id} did not reach {expected} within {timeout}s")
