from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check_reports_healthy_with_utc_time():
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "HEALTHY"
    datetime.strptime(body["current_time"], "%Y-%m-%dT%H:%M:%SZ")


def test_readiness_check_reaches_the_database(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "READY"


def test_readiness_check_reports_database_outage(client, monkeypatch):
    from app import main

    class BrokenEngine:
        def connect(self):
            raise ConnectionError("database unreachable")

    monkeypatch.setattr(main, "engine", BrokenEngine())

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "UNAVAILABLE"


def test_responses_report_server_timing():
    response = client.get("/")

    assert response.headers["Server-Timing"].startswith("app;dur=")
