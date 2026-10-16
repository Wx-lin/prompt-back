"""Explicit local-only PostgreSQL fixture for real admin UI verification."""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models import CrawlDraft, Prompt, PromptImage, User
from app.routers.admin_insights import router
from app.utils.admin_auth import verify_admin_key


@asynccontextmanager
async def lifespan(app):
    url = os.environ["AIGC_TEST_DATABASE_URL"]
    if url != "postgresql+asyncpg:///postgres":
        raise RuntimeError("This fixture requires the explicitly selected local socket database")
    schema = "test_insights_browser_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            now = datetime.now(UTC)
            db.add_all(
                [
                    User(
                        nickname=f"运营测试{i:02}",
                        email=f"operator{i:02}@example.com",
                        is_active=i % 2 == 0,
                        created_at=now - timedelta(minutes=i),
                        last_login_at=now if i == 0 else None,
                    )
                    for i in range(25)
                ]
            )
            db.add(
                User(
                    email="literal%_@example.com",
                    nickname=None,
                    is_active=True,
                    created_at=now - timedelta(days=2),
                )
            )
            for category, published, status in [
                ("portrait", True, None),
                ("poster", True, "approved"),
                ("portrait", True, "pending"),
                ("poster", False, "rejected"),
                ("workflow", False, None),
            ]:
                db.add(
                    Prompt(
                        title="浏览器测试",
                        prompt_text="test",
                        category=category,
                        is_published=published,
                        review_status=status,
                        images=[PromptImage(image_url="https://example.com/test.jpg")],
                    )
                )
            db.add(
                CrawlDraft(
                    source_platform="test",
                    source_note_id="fixture",
                    source_url="https://example.com",
                    raw_content="test",
                    status="pending",
                )
            )
            await db.commit()

        async def local_db():
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db] = local_db
        yield
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(router)

# Deliberately use a local-only test identity, never a production administrator secret.


async def test_admin_key(x_admin_key: str | None = Header(None)):
    if x_admin_key != "test-insights-browser":
        raise HTTPException(401, "Test administrator required")


app.dependency_overrides[verify_admin_key] = test_admin_key
