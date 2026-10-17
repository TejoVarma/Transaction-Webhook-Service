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

engine = create_async_engine(
    url,
    connect_args=connect_args,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session():
    async with SessionLocal() as session:
        yield session
