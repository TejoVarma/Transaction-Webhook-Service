from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.db import engine
from app.models import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="Transaction Webhook Service", lifespan=lifespan)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/")
async def health_check():
    return {"status": "HEALTHY", "current_time": utc_now_iso()}
