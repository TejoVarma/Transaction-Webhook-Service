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
