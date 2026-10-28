"""Text task validation and private HTTP lifecycle on isolated real services."""

import os
import runpy
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.models import PromptAnalysis, PromptAnalysisAsset
from app.schemas.prompt_analysis import CreateAnalysisRequest
from tests.test_prompt_api_integration import (
    api as api,
)
from tests.test_prompt_api_integration import (
    configured as configured,
)
from tests.test_prompt_api_integration import (
    create,
    generated_result,
    response_data,
    upload,
)
from tests.test_prompt_api_integration import (
    limiter as limiter,
)
from tests.test_prompt_api_integration import (
    objects as objects,
)
from tests.test_prompt_api_integration import (
    sessions as sessions,
)

TEXT = {
    "input_type": "text", "idea_text": "雨夜送花的小猫",
    "purpose": "wallpaper", "style": "cinematic",
}
integration = pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="Explicit isolated PostgreSQL and Redis test URLs required",
)


def test_text_input_normalizes_and_preserves_unicode():
    value = CreateAnalysisRequest.model_validate({**TEXT, "idea_text": "  雨夜的小猫 🐈\n  "})
    assert value.idea_text == "雨夜的小猫 🐈"
    assert value.asset_id is None
    assert value.input_type == "text"


@pytest.mark.parametrize("changes", [
    {"idea_text": "  "}, {"idea_text": "猫" * 501}, {"idea_text": 123},
    {"idea_text": None}, {"asset_id": str(uuid.uuid4())},
    {"purpose": "unknown"}, {"style": "unknown"}, {"extra": "unexpected"},
])
def test_text_input_rejects_invalid_and_mixed_fields(changes):
    with pytest.raises(ValidationError):
        CreateAnalysisRequest.model_validate({**TEXT, **changes})


def test_legacy_image_request_is_unchanged():
    asset = uuid.uuid4()
    value = CreateAnalysisRequest.model_validate({"asset_id": str(asset)})
    assert value.input_type == "image"
    assert value.asset_id == asset
    assert value.idea_text is None
    for fields in ({"idea_text": "猫"}, {"purpose": "wallpaper"}, {"style": "cinematic"}):
        with pytest.raises(ValidationError):
            CreateAnalysisRequest.model_validate({"asset_id": str(asset), **fields})


async def create_text(api, body=None, key=None, headers=None):
    return await api.client.post(
        "/api/prompt-analyses", json=body or TEXT,
        headers={**(headers or api.owner), "Idempotency-Key": key or str(uuid.uuid4())},
    )


@pytest.mark.asyncio
@integration
async def test_text_create_detail_history_edit_delete_without_cos(api, sessions, monkeypatch):
    async def unexpected_cos(*args, **kwargs):
        raise AssertionError("Text tasks must never access image assets or COS")

    from app.services.prompt_analysis import assets
    monkeypatch.setattr(assets, "owned_asset", unexpected_cos)
    monkeypatch.setattr(assets, "preview_fields", unexpected_cos)
    task = response_data(await create_text(api), 202)
    assert task["input_type"] == "text"
    assert task["asset_id"] is None
    assert task["idea_text"] == TEXT["idea_text"]
    assert task["purpose"] == "wallpaper" and task["style"] == "cinematic"
    assert not task["image_available"]
    assert task["thumbnail_url"] is None and task["preview_url"] is None
    assert not task["saved_to_history"]
    path = "/api/prompt-analyses/" + task["id"]
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(task["id"]))
        job.status = "succeeded"
        job.generated_result = generated_result()
        await db.commit()
        assert await db.scalar(select(PromptAnalysisAsset.id)) is None
    detail = response_data(await api.client.get(path, headers=api.owner))
    assert detail["saved_to_history"] and detail["result"] == generated_result()
    listing = response_data(await api.client.get("/api/prompt-analyses", headers=api.owner))
    assert listing["items"][0]["idea_text"] == TEXT["idea_text"]
    assert listing["items"][0]["input_type"] == "text"
    changed = {"zh": "修改后的中文", "en": "Updated English prompt"}
    edited = response_data(await api.client.patch(
        path, headers=api.owner, json={"revision": 0, "prompt": changed},
    ))
    assert edited["prompt"] == changed and edited["revision"] == 1
    assert edited["result"] == generated_result()
    response_data(await api.client.delete(path, headers=api.owner), 204)
    response_data(await api.client.get(path, headers=api.owner), 404)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(task["id"]))
        assert job.idea_text is None and job.generated_result is None


@pytest.mark.asyncio
@integration
async def test_text_idempotency_includes_options_and_preserves_replay(api, configured, limiter):
    key = str(uuid.uuid4())
    task = response_data(await create_text(api, key=key), 202)
    limiter.unavailable = True
    configured.prompt_analysis_enabled = False
    replay = response_data(await create_text(api, key=key))
    assert replay["id"] == task["id"]
    for changed in (
        {"idea_text": "换成白猫"}, {"purpose": "avatar"}, {"style": "anime"},
    ):
        error = response_data(await create_text(api, body={**TEXT, **changed}, key=key), 409)
        assert error["code"] == "IDEMPOTENCY_CONFLICT"
    response_data(await api.client.delete("/api/prompt-analyses/" + task["id"], headers=api.owner), 204)
    response_data(await create_text(api, key=key), 404)


@pytest.mark.asyncio
@integration
async def test_text_tasks_share_image_active_limit_and_are_private(api):
    asset = await upload(api)
    task = response_data(await create_text(api), 202)
    error = response_data(await create(api, asset["id"]), 409)
    assert error["code"] == "ANALYSIS_IN_PROGRESS"
    path = "/api/prompt-analyses/" + task["id"]
    response_data(await api.client.get(path, headers=api.other), 404)
    response_data(await api.client.patch(path, headers=api.other, json={
        "revision": 0, "prompt": {"zh": "私有", "en": "Private"},
    }), 404)
    response_data(await api.client.delete(path, headers=api.other), 404)
    other = response_data(await api.client.get("/api/prompt-analyses", headers=api.other))
    assert other["items"] == []
    response_data(await api.client.post("/api/prompt-analyses", json=TEXT), 401)


@pytest.mark.asyncio
@integration
async def test_text_quota_counts_and_survives_deletion(api, sessions, configured):
    configured.prompt_analysis_daily_limit = 1
    task = response_data(await create_text(api), 202)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(task["id"]))
        job.status = "failed"
        job.quota_state = "consumed"
        await db.commit()
    response_data(await api.client.delete("/api/prompt-analyses/" + task["id"], headers=api.owner), 204)
    error = response_data(await create_text(api), 429)
    assert error["code"] == "QUOTA_EXCEEDED"


@pytest.mark.asyncio
@integration
async def test_database_rejects_invalid_text_input_variants(api, sessions):
    task = response_data(await create_text(api), 202)
    for fields in (
        {"input_type": "other"}, {"idea_text": None}, {"idea_text": " "},
        {"idea_text": "猫" * 501}, {"purpose": "other"}, {"style": "other"},
    ):
        async with sessions() as db:
            with pytest.raises(IntegrityError):
                await db.execute(update(PromptAnalysis).where(
                    PromptAnalysis.id == uuid.UUID(task["id"]),
                ).values(**fields))
                await db.commit()
            await db.rollback()


def apply_input_migration(connection, name):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/f15c86be7423_text_prompt_input.py"
    migration = runpy.run_path(str(path))
    with Operations.context(MigrationContext.configure(connection)):
        migration[name]()


@pytest.mark.asyncio
@integration
async def test_input_migration_preserves_legacy_jobs_and_refuses_text_data_loss(api, sessions):
    asset = await upload(api)
    image = response_data(await create(api, asset["id"]), 202)
    engine = sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(lambda value: apply_input_migration(value, "downgrade"))
        columns = await connection.run_sync(lambda value: {
            item["name"] for item in inspect(value).get_columns("prompt_analyses")
        })
        assert "input_type" not in columns
        await connection.run_sync(lambda value: apply_input_migration(value, "upgrade"))
    restored = response_data(await api.client.get(
        "/api/prompt-analyses/" + image["id"], headers=api.owner,
    ))
    assert restored["input_type"] == "image" and restored["asset_id"] == asset["id"]
    response_data(await api.client.delete("/api/prompt-analyses/" + image["id"], headers=api.owner), 204)
    response_data(await create_text(api), 202)
    async with engine.begin() as connection:
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await connection.run_sync(lambda value: apply_input_migration(value, "downgrade"))
