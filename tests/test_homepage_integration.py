"""Homepage ordering, filters, optimistic saves and privacy on real PostgreSQL."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Prompt, PromptImage
from app.schemas.prompt import SortOption
from app.services.homepage_service import (
    FeaturedConflict,
    InvalidFeaturedSelection,
    get_featured,
    replace_featured,
)
from app.services.prompt_service import list_prompts

pytestmark = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit test DB required")


def test_curation_order_filters_conflicts_and_clear():
    async def run():
        schema = "test_homepage_" + uuid.uuid4().hex
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
                rows = [
                    Prompt(
                        title=f"猫{i}",
                        prompt_text="test",
                        category="portrait" if i < 3 else "poster",
                        models=["FLUX"],
                        like_count=i * 10,
                        is_published=i != 4,
                        images=[PromptImage(image_url=f"https://example.com/{i}.jpg")],
                    )
                    for i in range(5)
                ]
                db.add_all(rows)
                await db.commit()
                ids = [row.id for row in rows]
                empty = await get_featured(db)
                selected = await replace_featured(db, [ids[1], ids[0]], empty.revision)
                assert [item.id for item in selected.items] == [ids[1], ids[0]]
                items, total = await list_prompts(db, None, None, SortOption.RECOMMENDED, 1, 10)
                assert total == 4
                assert [item.id for item in items] == [ids[1], ids[0], ids[3], ids[2]]
                page, total = await list_prompts(db, "portrait", ["FLUX"], SortOption.RECOMMENDED, 2, 1, "猫")
                assert total == 3 and page[0].id == ids[0]
                popular, _ = await list_prompts(db, None, None, SortOption.POPULAR, 1, 10)
                assert popular[0].id == ids[3]
                with pytest.raises(FeaturedConflict):
                    await replace_featured(db, [], empty.revision)
                await db.rollback()
                with pytest.raises(InvalidFeaturedSelection):
                    await replace_featured(db, [ids[4]], selected.revision)
                await db.rollback()
                hidden = await db.get(Prompt, ids[1])
                hidden.is_published = False
                await db.commit()
                visible = await get_featured(db)
                assert [item.id for item in visible.items] == [ids[0]]
                cleared = await replace_featured(db, [], visible.revision)
                assert cleared.items == []
                fallback, _ = await list_prompts(db, None, None, SortOption.RECOMMENDED, 1, 10)
                assert fallback[0].id == ids[3]
                no_image = Prompt(
                    title="猫无图",
                    prompt_text="test",
                    category="portrait",
                    models=[],
                    like_count=999,
                    is_published=True,
                )
                db.add(no_image)
                await db.commit()
                candidates, candidate_total = await list_prompts(
                    db,
                    None,
                    None,
                    SortOption.POPULAR,
                    1,
                    1,
                    search="猫",
                    require_image=True,
                )
                assert candidate_total == 3 and candidates[0].id == ids[3]
                assert candidates[0].thumbnail_url == "https://example.com/3.jpg"
                with pytest.raises(InvalidFeaturedSelection):
                    await replace_featured(db, [no_image.id], cleared.revision)
                await db.rollback()
                async with sessions() as other:
                    await db.execute(text("SELECT pg_advisory_xact_lock(731041302)"))
                    waiting = asyncio.create_task(replace_featured(other, [], cleared.revision))
                    await asyncio.sleep(0.05)
                    assert not waiting.done()
                    await replace_featured(db, [ids[0]], cleared.revision)
                    with pytest.raises(FeaturedConflict):
                        await waiting
                    await other.rollback()
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())


def test_homepage_migration_is_additive_and_reversible():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "alembic/versions/f8b24d0c51e3_homepage_rank.py"
    spec = importlib.util.spec_from_file_location("homepage_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async def run():
        schema = "test_migration_" + uuid.uuid4().hex
        engine = create_async_engine(os.environ["AIGC_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await conn.execute(text("CREATE TABLE prompts (id INTEGER PRIMARY KEY)"))
                await conn.execute(text("INSERT INTO prompts(id) VALUES (1)"))

                def apply(sync_conn, direction):
                    with Operations.context(MigrationContext.configure(sync_conn)):
                        getattr(migration, direction)()

                await conn.run_sync(apply, "upgrade")
                assert (await conn.execute(text("SELECT homepage_rank FROM prompts"))).scalar_one() is None
                await conn.execute(text("INSERT INTO prompts(id) VALUES (2)"))
                await conn.run_sync(apply, "downgrade")
                assert (await conn.execute(text("SELECT COUNT(*) FROM prompts"))).scalar_one() == 2
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()

    asyncio.run(run())
