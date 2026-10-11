"""Opt-in PostgreSQL tests using a disposable schema, never production tables."""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import CrawlDraft, CrawlDraftImage, Prompt, PromptImage
from app.services import auto_review_service as service
from app.services.content_review_service import ContentReviewResult, ImageReviewResult

pytestmark = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Requires explicit test DB")


def test_real_publication_is_atomic_and_idempotent(monkeypatch):
    review = ContentReviewResult(
        images=[ImageReviewResult(0, True, "AI"), ImageReviewResult(1, False, "photo")],
        suggested_title="水墨山水",
        cleaned_prompt_text="水墨山水，远山薄雾",
        suggested_category="illustration",
    )
    monkeypatch.setattr(service, "_upload_images", AsyncMock(return_value=["https://cos/a", "https://cos/b"]))
    monkeypatch.setattr(
        service.content_review_service, "review_draft_content", AsyncMock(return_value=review)
    )

    async def run():
        schema = "test_aigc_" + uuid.uuid4().hex
        admin = create_async_engine(os.environ["AIGC_TEST_DATABASE_URL"])
        engine = create_async_engine(
            os.environ["AIGC_TEST_DATABASE_URL"],
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                draft = CrawlDraft(
                    source_platform="xiaohongshu",
                    source_note_id="test",
                    source_url="https://www.xiaohongshu.com/explore/test",
                    raw_content="原始文案",
                    author_name="作者",
                    status="pending",
                )
                db.add(draft)
                await db.flush()
                db.add(CrawlDraftImage(draft_id=draft.id, local_path="a.jpg", sort_order=0))
                db.add(CrawlDraftImage(draft_id=draft.id, local_path="b.jpg", sort_order=1))
                await db.commit()
                draft_id = draft.id
            async with sessions() as db:
                assert await service.auto_publish_draft(db, draft_id) == "published"
            async with sessions() as db:
                assert await service.auto_publish_draft(db, draft_id) == "skipped"
                assert (await db.scalar(select(func.count()).select_from(Prompt))) == 1
                assert (await db.scalar(select(func.count()).select_from(PromptImage))) == 1
                saved = await db.get(CrawlDraft, draft_id)
                published = await db.get(Prompt, saved.approved_prompt_id)
                assert saved.status == "approved"
                assert published.source_url.endswith("/test")
                assert published.author_name == "作者"
                assert published.is_published
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())
