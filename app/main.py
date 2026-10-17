import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db import engine
from app.models import Base
from app.routes import router
from app.timeutils import utc_now_iso
from app.worker import TransactionWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    worker = TransactionWorker(
        processing_delay=settings.processing_delay_seconds,
        concurrency=settings.worker_concurrency,
        idle_poll_seconds=settings.worker_idle_poll_seconds,
        max_attempts=settings.max_attempts,
    )
    app.state.worker = worker
    worker.start()

    yield

    await worker.stop()
    await engine.dispose()


app = FastAPI(title="Transaction Webhook Service", lifespan=lifespan)
app.include_router(router)


@app.get("/")
async def health_check():
    return {"status": "HEALTHY", "current_time": utc_now_iso()}
