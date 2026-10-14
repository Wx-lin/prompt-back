"""Audited plans never infer approval or overwrite content."""

import importlib.util
import uuid
from copy import deepcopy

import pytest


def service():
    assert importlib.util.find_spec("app.services.category_migration_service") is not None, (
        "migration service missing"
    )
    from app.services import category_migration_service

    return category_migration_service


def fixture():
    record_id = str(uuid.uuid4())
    snapshot = {
        "prompts": [
            {
                "id": record_id,
                "title": "商品图",
                "prompt_text": "商品场景",
                "description": None,
                "category": "photography",
                "is_published": True,
                "review_status": None,
                "updated_at": "2026-10-14T01:00:00+00:00",
            }
        ],
        "prompt_images": [],
        "crawl_drafts": [],
        "crawl_draft_images": [],
    }
    reviews = [
        {
            "entity": "prompt",
            "id": record_id,
            "old_category": "photography",
            "new_category": "product",
            "confirmed": False,
        }
    ]
    return snapshot, reviews


def test_unconfirmed_rows_do_not_become_applicable():
    snapshot, reviews = fixture()
    plan = service().prepare_plan(snapshot, reviews)
    assert plan["applicable"] == []
    assert plan["summary"]["unconfirmed"] == 1
    assert plan["summary"]["unresolved_public"] == 1


def test_confirmed_plan_is_category_only_and_does_not_mutate_input():
    snapshot, reviews = fixture()
    reviews = [{**reviews[0], "confirmed": True}]
    original = deepcopy((snapshot, reviews))
    plan = service().prepare_plan(snapshot, reviews)
    assert len(plan["applicable"]) == 1
    row = plan["applicable"][0]
    assert row["before_state"]["category"] == "photography"
    assert row["after_category"] == "product"
    assert (snapshot, reviews) == original


@pytest.mark.parametrize(
    "patch",
    [
        {"new_category": "workflow"},
        {"new_category": "nonsense"},
        {"confirmed": "true"},
        {"entity": "users"},
        {"id": "not-a-uuid"},
        {"is_published": False},
    ],
)
def test_invalid_or_unapproved_state_changes_fail_closed(patch):
    snapshot, reviews = fixture()
    with pytest.raises(ValueError):
        service().prepare_plan(snapshot, [{**reviews[0], "confirmed": True, **patch}])


def test_duplicate_or_missing_id_rejected():
    snapshot, reviews = fixture()
    with pytest.raises(ValueError):
        service().prepare_plan(snapshot, reviews + reviews)
    with pytest.raises(ValueError):
        service().prepare_plan(snapshot, [{**reviews[0], "id": str(uuid.uuid4())}])


def test_unknown_historical_keys_reported_not_silently_supported():
    snapshot, reviews = fixture()
    snapshot["prompts"][0]["category"] = "unknown_history"
    reviews[0] = {**reviews[0], "old_category": "unknown_history", "confirmed": True}
    plan = service().prepare_plan(snapshot, reviews)
    assert plan["applicable"] == []
    assert plan["summary"]["unknown_keys"] == ["unknown_history"]


def test_pending_draft_is_guarded_by_status_and_source_content():
    snapshot, reviews = fixture()
    record_id = str(uuid.uuid4())
    snapshot["crawl_drafts"] = [
        {
            "id": record_id,
            "raw_title": "建筑",
            "raw_content": "室内效果图",
            "extracted_category": None,
            "extracted_prompt_text": None,
            "status": "pending",
            "reviewed_at": None,
        }
    ]
    row = {
        "entity": "draft",
        "id": record_id,
        "old_category": None,
        "new_category": "architecture",
        "confirmed": True,
    }
    state = service().prepare_plan(snapshot, [row])["applicable"][0]["before_state"]
    assert state["status"] == "pending" and state["raw_content"] == "室内效果图"
    snapshot["crawl_drafts"][0]["status"] = "approved"
    assert not service().prepare_plan(snapshot, [row])["applicable"]


def test_public_preview_counts_missing_review_entries():
    snapshot, reviews = fixture()
    plan = service().prepare_plan(snapshot, [])
    assert plan["summary"]["unresolved_public"] == 1
    assert not plan["summary"]["catalog_switch_ready"]


def test_snapshot_category_conflict_rejected():
    snapshot, reviews = fixture()
    with pytest.raises(ValueError):
        service().prepare_plan(snapshot, [{**reviews[0], "old_category": "portrait"}])


def test_state_version_ignores_counters_but_guards_text_and_images():
    snapshot, reviews = fixture()
    before = service().record_state("prompt", snapshot["prompts"][0], [])
    changed = {
        **snapshot["prompts"][0],
        "like_count": 50,
        "view_count": 900,
        "updated_at": "2026-10-14T02:00:00+00:00",
    }
    assert service().record_state("prompt", changed, []) == before
    changed = {**changed, "prompt_text": "人工重写"}
    assert service().record_state("prompt", changed, []) != before


def test_real_apply_retry_concurrent_edit_and_safe_rollback():
    import asyncio
    import os

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import CategoryLegacyMembership, CategoryMigrationChange, Prompt, PromptImage

    if not os.getenv("AIGC_TEST_DATABASE_URL"):
        pytest.skip("Explicit disposable test database required")

    async def run():
        schema = "test_category_migration_" + uuid.uuid4().hex
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
                a = Prompt(
                    title="商品场景",
                    prompt_text="product",
                    category="photography",
                    models=[],
                    is_published=True,
                    images=[PromptImage(image_url="https://example.com/a")],
                )
                b = Prompt(
                    title="壁纸", prompt_text="wallpaper", category="image_edit", models=[], is_published=True
                )
                db.add_all([a, b])
                await db.commit()
                snapshot = await service().export_snapshot(db)
                ids = [str(a.id), str(b.id)]
            reviews = [
                {
                    "entity": "prompt",
                    "id": ids[0],
                    "old_category": "photography",
                    "new_category": "product",
                    "confirmed": True,
                },
                {
                    "entity": "prompt",
                    "id": ids[1],
                    "old_category": "image_edit",
                    "new_category": "wallpaper",
                    "confirmed": True,
                },
            ]
            plan = service().prepare_plan(snapshot, reviews)
            async with sessions() as db:
                a = await db.get(Prompt, uuid.UUID(ids[0]))
                a.like_count += 1
                b = await db.get(Prompt, uuid.UUID(ids[1]))
                b.prompt_text = "人工修改"
                await db.commit()
            async with sessions() as db:
                result = await service().apply_plan(db, plan, "test-batch")
                assert result["applied"] == 1 and result["concurrent_skipped"] == 1
            async with sessions() as db:
                assert (await db.get(Prompt, uuid.UUID(ids[0]))).category == "product"
                assert (await db.get(Prompt, uuid.UUID(ids[1]))).prompt_text == "人工修改"
                assert await db.get(CategoryLegacyMembership, ("photography", uuid.UUID(ids[0])))
                assert (await db.scalars(select(CategoryMigrationChange))).all()
            async with sessions() as db:
                retry = await service().apply_plan(db, plan, "test-batch")
                assert retry["applied"] == 0 and retry["already_applied"] == 1
            async with sessions() as db:
                a = await db.get(Prompt, uuid.UUID(ids[0]))
                a.view_count += 1
                await db.commit()
            async with sessions() as db:
                result = await service().rollback_batch(db, "test-batch")
                assert result["restored"] == 1
            async with sessions() as db:
                assert (await db.get(Prompt, uuid.UUID(ids[0]))).category == "photography"
            async with sessions() as db:
                snapshot = await service().export_snapshot(db)
            plan = service().prepare_plan(snapshot, reviews)
            async with sessions() as db:
                assert (await service().apply_plan(db, plan, "test-batch-2"))["applied"] == 2
            async with sessions() as db:
                a = await db.get(Prompt, uuid.UUID(ids[0]))
                a.title = "后来人工编辑"
                await db.commit()
            async with sessions() as db:
                rollback = await service().rollback_batch(db, "test-batch-2")
                assert rollback["restored"] == 1 and rollback["concurrent_skipped"] == 1
            async with sessions() as db:
                a = await db.get(Prompt, uuid.UUID(ids[0]))
                assert a.title == "后来人工编辑" and a.category == "product"
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())


def test_category_schema_upgrade_and_downgrade_preserve_prompt_records():
    import asyncio
    import importlib.util
    import os
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    if not os.getenv("AIGC_TEST_DATABASE_URL"):
        pytest.skip("Explicit disposable test database required")
    path = Path(__file__).parents[1] / "alembic/versions/9c35e1a62f4_category_migration.py"
    spec = importlib.util.spec_from_file_location("category_revision", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async def run():
        schema = "test_category_schema_" + uuid.uuid4().hex
        engine = create_async_engine(os.environ["AIGC_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await conn.execute(text("CREATE TABLE prompts (id UUID PRIMARY KEY, category TEXT NOT NULL)"))
                await conn.execute(
                    text("INSERT INTO prompts(id,category) VALUES (:id,:category)"),
                    {"id": uuid.uuid4(), "category": "workflow"},
                )

                def apply(connection, direction):
                    with Operations.context(MigrationContext.configure(connection)):
                        getattr(migration, direction)()

                await conn.run_sync(apply, "upgrade")
                assert await conn.scalar(text("SELECT COUNT(*) FROM category_legacy_memberships")) == 0
                assert await conn.scalar(text("SELECT COUNT(*) FROM category_migration_changes")) == 0
                assert await conn.scalar(text("SELECT category FROM prompts")) == "workflow"
                await conn.run_sync(apply, "downgrade")
                assert await conn.scalar(text("SELECT COUNT(*) FROM prompts")) == 1
                assert await conn.scalar(text("SELECT category FROM prompts")) == "workflow"
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_category_revision_is_compatible_with_deployment_revision_gate():
    import importlib.util
    import re
    from pathlib import Path

    path = next((Path(__file__).parents[1] / "alembic/versions").glob("*category_migration.py"))
    spec = importlib.util.spec_from_file_location("category_revision_gate", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert re.fullmatch(r"[0-9a-f]+", migration.revision)
