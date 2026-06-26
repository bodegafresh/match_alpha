from collections.abc import AsyncIterator
from uuid import uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.core.config import get_settings


def create_engine() -> AsyncEngine | None:
    settings = get_settings()
    if not settings.database_url:
        return None
    database_url = make_url(settings.database_url).update_query_dict({"prepared_statement_cache_size": "0"})
    return create_async_engine(
        database_url,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        },
        poolclass=NullPool,
        pool_pre_ping=True,
        future=True,
    )


engine = create_engine()


async def get_connection() -> AsyncIterator[AsyncConnection]:
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with engine.begin() as conn:
        yield conn
