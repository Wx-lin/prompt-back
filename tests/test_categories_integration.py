"""Real PostgreSQL visibility and legacy filtering before pagination."""

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import CategoryLegacyMembership, CrawlDraft, Prompt
from app.routers.categories import list_categories
from app.schemas.admin import AdminPromptOut
from app.schemas.crawl import CrawlDraftOut
from app.schemas.prompt import SortOption
from app.services import admin_service, submission_service
from app.services.prompt_service import get_prompt_detail, get_similar_prompts, list_prompts

pytestmark = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Requires explicit test DB")


def test_public_and_legacy_filters():
    async def run():
        schema = "test_categories_" + uuid.uuid4().hex
        admin = create_async_engine(os.environ["AIGC_TEST_DATABASE_URL"])
        engine = create_async_engine(
            os.environ["AIGC_TEST_DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}}
        )
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                records = []
                for category, status, published, likes in [
                    ("portrait", None, True, 1),
                    ("product", "approved", True, 2),
                    ("portrait", "pending", True, 90),
                    ("portrait", "rejected", True, 99),
                    ("portrait", None, False, 100),
                ]:
                    row = Prompt(
                        title="猫",
                        prompt_text="x",
                        category=category,
                        models=["MJ V6"],
                        review_status=status,
                        is_published=published,
                        like_count=likes,
                    )
                    db.add(row)
                    records.append(row)
                await db.flush()
                for row in records:
                    db.add(CategoryLegacyMembership(legacy_key="photography", prompt_id=row.id))
                await db.commit()
                catalog = await list_categories(db)
                assert len(catalog) == 12 and sum(r["count"] for r in catalog) == 2
                assert next(r for r in catalog if r["key"] == "sticker")["count"] == 0
                one, total = await list_prompts(
                    db, "photography", ["Midjourney V6"], SortOption.POPULAR, 1, 1, search="猫"
                )
                two, total2 = await list_prompts(
                    db, "photography", ["Midjourney V6"], SortOption.POPULAR, 2, 1, search="猫"
                )
                assert total == total2 == 2 and one[0].id == records[1].id and two[0].id == records[0].id
                assert await list_prompts(db, "workflow", None, SortOption.LATEST, 1, 20) == ([], 0)
                with pytest.raises(HTTPException) as error:
                    await list_prompts(db, "invalid", None, SortOption.LATEST, 1, 20)
                assert error.value.status_code == 422
                for row in records[2:]:
                    assert await get_prompt_detail(db, row.id) is None
                filtered, filtered_total = await admin_service.list_prompts_for_admin(
                    db, None, 1, 20, category="product"
                )
                assert filtered_total == 1 and filtered[0].id == records[1].id
                pending, pending_total = await submission_service.list_submissions(
                    db, None, "pending", 1, 20, category="portrait"
                )
                assert pending_total == 1 and pending[0].id == records[2].id
                similar = await get_similar_prompts(db, uuid.uuid4(), "portrait")
                assert [r.id for r in similar] == [records[0].id]
                corrected = await submission_service.review_submission(
                    db, records[2].id, True, category="sticker"
                )
                assert corrected.category == "sticker" and corrected.review_status == "approved"
                with pytest.raises(submission_service.SubmissionConflict):
                    await submission_service.review_submission(db, records[2].id, True, category="product")
                assert corrected.category == "sticker"
                for key in ("photography", "image_edit", "workflow", "unknown_historical"):
                    legacy = Prompt(
                        title="历史", prompt_text="正文", category=key, models=[], is_published=False
                    )
                    draft = CrawlDraft(
                        source_platform="xhs",
                        source_note_id=key,
                        source_url="https://example.com",
                        raw_content="正文",
                        extracted_category=key,
                        status="pending",
                    )
                    db.add_all([legacy, draft])
                    await db.flush()
                    await db.refresh(legacy)
                    await db.refresh(draft, attribute_names=["images"])
                    assert AdminPromptOut.model_validate(legacy).model_dump(mode="json")["category"] == key
                    assert (
                        CrawlDraftOut.model_validate(draft).model_dump(mode="json")["extracted_category"]
                        == key
                    )
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())
