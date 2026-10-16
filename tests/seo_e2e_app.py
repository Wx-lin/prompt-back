"""Local-only isolated PostgreSQL browser fixture; never loads production records."""

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_db
from app.models.seo import SeoSnapshot
from app.routers.seo import router
from app.utils.admin_auth import verify_admin_key


@asynccontextmanager
async def lifespan(app):
    url = os.environ["AIGC_TEST_DATABASE_URL"]
    if url != "postgresql+asyncpg:///aigc_seo_test_20261016":
        raise RuntimeError("Explicit local isolated test database required")
    schema = "test_seo_browser_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: SeoSnapshot.__table__.create(c))
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def local_db():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_db] = local_db
        yield
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def local_key(x_admin_key: str | None = Header(None)):
    if x_admin_key != "test-seo-browser":
        raise HTTPException(401, "Test administrator required")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.dependency_overrides[verify_admin_key] = local_key
