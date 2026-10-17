import asyncio
from decimal import Decimal

import httpx
import pytest

from app.main import app
from tests.helpers import WEBHOOK_URL, count_rows, fetch_row, make_payload


def test_webhook_is_accepted_and_stored(client):
    response = client.post(WEBHOOK_URL, json=make_payload())

    assert response.status_code == 202
    assert response.json() == {
        "status": "ACCEPTED",
        "transaction_id": "txn_test_1",
        "duplicate": False,
    }
    row = client.portal.call(fetch_row, "txn_test_1")
    assert row["status"] == "PROCESSING"
    assert row["amount"] == Decimal("1500.00")
    assert row["processed_at"] is None


def test_repeated_webhook_is_stored_once(client):
    flags = [client.post(WEBHOOK_URL, json=make_payload()).json()["duplicate"] for _ in range(3)]

    assert flags == [False, True, True]
    assert client.portal.call(count_rows) == 1


def test_concurrent_duplicates_create_exactly_one_transaction(client):
    async def send_all_at_once():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await asyncio.gather(
                *(http.post(WEBHOOK_URL, json=make_payload("txn_race")) for _ in range(10))
            )

    responses = client.portal.call(send_all_at_once)

    assert all(r.status_code == 202 for r in responses)
    assert sum(not r.json()["duplicate"] for r in responses) == 1
    assert client.portal.call(count_rows) == 1


def test_currency_is_normalised(client):
    client.post(WEBHOOK_URL, json=make_payload(currency=" usd "))

    assert client.portal.call(fetch_row, "txn_test_1")["currency"] == "USD"


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": -5},
        {"amount": 0},
        {"amount": 10.123},
        {"amount": "lots"},
        {"currency": "RUPEES"},
        {"currency": "IN"},
        {"transaction_id": ""},
        {"source_account": None},
    ],
)
def test_invalid_payload_is_rejected(client, overrides):
    response = client.post(WEBHOOK_URL, json=make_payload(**overrides))

    assert response.status_code == 422
    assert client.portal.call(count_rows) == 0


def test_missing_field_is_rejected(client):
    payload = make_payload()
    del payload["amount"]

    assert client.post(WEBHOOK_URL, json=payload).status_code == 422
