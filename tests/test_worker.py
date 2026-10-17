from datetime import timedelta

from app.config import settings
from tests.helpers import (
    WEBHOOK_URL,
    fetch_row,
    insert_abandoned,
    make_payload,
    wait_for_status,
)


def test_transaction_is_processed_after_the_delay(client):
    client.post(WEBHOOK_URL, json=make_payload())

    transaction = wait_for_status(client, "txn_test_1", "PROCESSED")

    assert transaction["processed_at"] is not None
    row = client.portal.call(fetch_row, "txn_test_1")
    assert row["processed_at"] - row["created_at"] >= timedelta(
        seconds=settings.processing_delay_seconds
    )
    assert row["attempts"] == 1
    assert row["locked_until"] is None


def test_abandoned_transaction_is_recovered(client):
    # A worker claimed this row and then died: its lease has already expired.
    client.portal.call(insert_abandoned, "txn_orphan", -1)
    client.portal.call(client.app.state.worker.notify)

    wait_for_status(client, "txn_orphan", "PROCESSED")

    assert client.portal.call(fetch_row, "txn_orphan")["attempts"] == 2


def test_transaction_leased_by_another_worker_is_left_alone(client):
    client.portal.call(insert_abandoned, "txn_busy", 3600)
    client.portal.call(client.app.state.worker.notify)

    wait_seconds = settings.processing_delay_seconds + 1.5
    client.portal.call(__import__("asyncio").sleep, wait_seconds)

    row = client.portal.call(fetch_row, "txn_busy")
    assert row["status"] == "PROCESSING"
    assert row["attempts"] == 1


def test_failed_attempt_is_retried(client, monkeypatch):
    worker = client.app.state.worker
    real_call = worker._call_external_service
    calls = []

    async def flaky(transaction_id):
        calls.append(transaction_id)
        if len(calls) == 1:
            raise ConnectionError("gateway timeout")
        await real_call(transaction_id)

    monkeypatch.setattr(worker, "_call_external_service", flaky)

    client.post(WEBHOOK_URL, json=make_payload())

    wait_for_status(client, "txn_test_1", "PROCESSED", timeout=30)
    row = client.portal.call(fetch_row, "txn_test_1")
    assert row["attempts"] == 2
    assert row["last_error"] is None


def test_transaction_fails_after_max_attempts(client, monkeypatch):
    worker = client.app.state.worker

    async def always_down(transaction_id):
        raise ConnectionError("gateway unreachable")

    monkeypatch.setattr(worker, "_call_external_service", always_down)
    monkeypatch.setattr(worker, "max_attempts", 1)

    client.post(WEBHOOK_URL, json=make_payload())

    transaction = wait_for_status(client, "txn_test_1", "FAILED")
    assert transaction["processed_at"] is None
    row = client.portal.call(fetch_row, "txn_test_1")
    assert "gateway unreachable" in row["last_error"]
