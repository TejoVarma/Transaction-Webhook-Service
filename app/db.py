from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings


def to_asyncpg_url(raw_url: str) -> tuple[URL, dict]:
    # asyncpg rejects libpq-only query params like sslmode/channel_binding,
    # so strip them and pass SSL through connect_args instead.
    url = make_url(raw_url)
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)

    connect_args = {}
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = "require"

    return url.set(drivername="postgresql+asyncpg", query=query), connect_args


url, connect_args = to_asyncpg_url(settings.database_url)

# Every API request is a single statement (one INSERT or one SELECT), which
# Postgres already runs atomically, so autocommit saves the BEGIN/COMMIT round
# trips. Instead of pinging before each checkout, connections are recycled
# before Neon's 5-minute idle suspend could have killed them.
engine = create_async_engine(
    url,
    connect_args=connect_args,
    isolation_level="AUTOCOMMIT",
    pool_size=40,
    max_overflow=20,
    pool_pre_ping=False,
    pool_recycle=240,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# The worker gets its own small pool so a burst of background work can never
# take connections away from the webhook endpoint. Every worker query is also a
# single atomic statement, so the same autocommit/recycle settings apply.
worker_engine = create_async_engine(
    url,
    connect_args=connect_args,
    isolation_level="AUTOCOMMIT",
    pool_size=10,
    max_overflow=10,
    pool_pre_ping=False,
    pool_recycle=240,
)

WorkerSessionLocal = async_sessionmaker(worker_engine, expire_on_commit=False)


async def get_session():
    async with SessionLocal() as session:
        yield session
