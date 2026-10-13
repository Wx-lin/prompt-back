"""Real PostgreSQL array filtering, pagination and version-preserving catalog."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Prompt
from app.routers.prompts import search_prompts
from app.schemas.admin import AdminPromptCreate, AdminPromptUpdate
from app.schemas.prompt import PromptDetailOut, SortOption
from app.services import admin_service, model_service, prompt_service

pytestmark = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Requires explicit test DB")


def test_catalog_filters_search_and_admin_write():
    async def run():
        schema = "test_models_" + uuid.uuid4().hex
        url = os.environ["AIGC_TEST_DATABASE_URL"]
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                for label, likes, category, published in [
                    ("MJ V6", 1, "portrait", True),
                    ("Midjourney v6", 9, "portrait", True),
                    ("MJ V7", 20, "portrait", True),
                    ("MJ V6", 30, "poster", True),
                    ("Secret Model", 50, "portrait", False),
                    ("Flux.1", 0, "portrait", True),
                ]:
                    db.add(
                        Prompt(
                            title="猫咪",
                            prompt_text="test",
                            category=category,
                            models=[label],
                            like_count=likes,
                            is_published=published,
                        )
                    )
                await db.commit()
                items, total = await prompt_service.list_prompts(
                    db, "portrait", ["Midjourney V6"], SortOption.POPULAR, 1, 1, search="猫"
                )
                assert total == 2
                assert items[0].like_count == 9
                assert items[0].models == ["Midjourney V6"]
                page2, total2 = await prompt_service.list_prompts(
                    db, "portrait", ["MJ V6"], SortOption.POPULAR, 2, 1, search="猫"
                )
                assert total2 == 2 and page2[0].like_count == 1
                response = await search_prompts(
                    q="猫",
                    category="portrait",
                    models=["MJ V6"],
                    sort=SortOption.POPULAR,
                    page=1,
                    page_size=1,
                    db=db,
                )
                assert response.total == 2 and response.has_more
                assert response.items[0].models == ["Midjourney V6"]
                empty, count = await prompt_service.list_prompts(
                    db, None, ["not-a-model"], SortOption.LATEST, 1, 20
                )
                assert empty == [] and count == 0
                catalog = await model_service.list_models(db)
                assert {"key": "Midjourney V6", "label": "Midjourney V6", "group": "Midjourney"} in catalog
                assert {"key": "FLUX.1", "label": "FLUX.1", "group": "FLUX"} in catalog
                assert len([item for item in catalog if item["key"] == "Midjourney V6"]) == 1
                assert not any(item["key"] == "Secret Model" for item in catalog)
                saved = await admin_service.create_prompt(
                    db,
                    AdminPromptCreate(
                        title="test",
                        prompt_text="test",
                        category="portrait",
                        models=["MJ V6", "midjourney v6"],
                    ),
                )
                assert saved.models == ["Midjourney V6"]
                updated = await admin_service.update_prompt(
                    db, saved.id, AdminPromptUpdate(models=["Flux.1"])
                )
                assert updated.models == ["FLUX.1"]
                assert PromptDetailOut.model_validate(updated).models == ["FLUX.1"]
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())
