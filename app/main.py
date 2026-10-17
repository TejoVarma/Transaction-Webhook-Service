import logging
import math
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db import engine, worker_engine
from app.models import Base
from app.ratelimit import TokenBucketLimiter
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
    await worker_engine.dispose()
    await engine.dispose()


app = FastAPI(title="Transaction Webhook Service", lifespan=lifespan)
app.state.rate_limiter = TokenBucketLimiter(
    rate=settings.rate_limit_per_second, burst=settings.rate_limit_burst
)
app.include_router(router)


@app.middleware("http")
async def server_timing(request: Request, call_next):
    # Reports time spent inside the app, so it can be told apart from network
    # and proxy time when measuring from outside (visible in browser devtools too).
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
    return response


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    # The health check stays unlimited so uptime pings are never refused.
    if settings.rate_limit_enabled and request.url.path.startswith("/v1/"):
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = request.app.state.rate_limiter.allow(client_ip)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests, slow down"},
                headers={"Retry-After": str(math.ceil(retry_after))},
            )
    return await call_next(request)


@app.get("/")
async def health_check():
    return {"status": "HEALTHY", "current_time": utc_now_iso()}


@app.get("/ready")
async def readiness_check():
    # Unlike "/", this touches the database, so an uptime ping here also keeps
    # a scale-to-zero database (Neon) awake.
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        logging.getLogger(__name__).exception("Readiness check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "UNAVAILABLE", "current_time": utc_now_iso()},
        )
    return {"status": "READY", "current_time": utc_now_iso()}
