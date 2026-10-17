import os

from dotenv import dotenv_values

# Point the app at the test database before anything imports app.config.
_env_file = dotenv_values(".env")
_test_url = os.environ.get("TEST_DATABASE_URL") or _env_file.get("TEST_DATABASE_URL")
_real_url = os.environ.get("DATABASE_URL") or _env_file.get("DATABASE_URL")

if not _test_url:
    raise RuntimeError("Set TEST_DATABASE_URL (a separate, disposable database) to run the tests")
if _test_url == _real_url:
    raise RuntimeError("TEST_DATABASE_URL must not be the same as DATABASE_URL")

os.environ["DATABASE_URL"] = _test_url
os.environ["PROCESSING_DELAY_SECONDS"] = "2"
os.environ["WORKER_IDLE_POLL_SECONDS"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402


async def _clear_transactions():
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE transactions"))


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.portal.call(_clear_transactions)
        yield test_client
