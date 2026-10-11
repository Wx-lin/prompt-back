"""Cross-process crawler exclusivity; dedicated non-pooled PostgreSQL connection."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

CRAWL_LOCK_ID = 724001091


@asynccontextmanager
async def crawl_lock():
    # Session advisory locks must never leak into the application's connection pool.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            acquired = bool(
                (
                    await connection.execute(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": CRAWL_LOCK_ID}
                    )
                ).scalar_one()
            )
            await connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CRAWL_LOCK_ID})
                    await connection.commit()
    finally:
        await engine.dispose()
