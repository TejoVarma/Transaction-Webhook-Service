from datetime import datetime

from tests.helpers import WEBHOOK_URL, make_payload


def test_unknown_transaction_returns_404(client):
    response = client.get("/v1/transactions/txn_does_not_exist")

    assert response.status_code == 404


def test_lookup_matches_spec_shape_while_processing(client):
    client.post(WEBHOOK_URL, json=make_payload(amount=150.50, currency="USD"))

    response = client.get("/v1/transactions/txn_test_1")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) and len(body) == 1
    transaction = body[0]
    assert transaction == {
        "transaction_id": "txn_test_1",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 150.5,
        "currency": "USD",
        "status": "PROCESSING",
        "created_at": transaction["created_at"],
        "processed_at": None,
    }
    datetime.strptime(transaction["created_at"], "%Y-%m-%dT%H:%M:%SZ")
