"""Isolated DB/Redis tests; model and COS transports are local fakes."""

import asyncio
import io
import os
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select, update

from app.models import PromptAnalysis, PromptAnalysisAsset
from app.services.prompt_analysis import assets, storage
from app.services.prompt_analysis.api import budget_lock
from app.services.prompt_analysis.cleanup import cleanup_assets
from app.services.prompt_analysis.errors import AnalysisError
from app.services.vision import image_adapter
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import image_bytes, response_data
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation, send_message

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="Explicit isolated PostgreSQL and Redis URLs required",
)]


async def test_image_turns_are_private_and_editable(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    original = image_bytes()
    alternate = io.BytesIO()
    Image.new("RGB", (96, 64), "red").save(alternate, format="PNG")
    provider = AsyncMock(side_effect=[original, alternate.getvalue(), original])
    monkeypatch.setattr(image_adapter, "generate", provider)
    key = str(uuid4())
    body = {"message": "一只猫", "mode": "image"}
    first = response_data(await create_conversation(api, body, key=key), 202)
    assert first["generation_mode"] == "image" and first["generated_image"] is None
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert first["response_type"] == "image" and first["generated_image"]["url"].startswith("https://")
    assert first["image_status"] == "available"
    assert first["prompt"] is None and first["result"] is None and first["generated_image"]["width"] == 96
    assert provider.await_args.kwargs["reference"] is None
    response_data(await create_conversation(api, body, key=key))
    assert provider.await_count == 1
    foreign = await send_message(api, first, "修改", mode="image", generated_image_id=str(uuid4()))
    assert foreign.status_code == 404
    second = response_data(await send_message(api, first, "换成蓝天", mode="image"), 202)
    assert await run_once(sessions)
    second = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert provider.await_args.kwargs["reference"].startswith(b"\x89PNG\r\n\x1a\n")
    assert "一只猫" in provider.await_args.args[0] and "换成蓝天" in provider.await_args.args[0]
    url = f"/api/prompt-conversations/{first['id']}/images/{first['generated_image']['id']}/download"
    downloaded = await api.client.get(url, headers=api.owner)
    assert downloaded.status_code == 200 and downloaded.headers["content-type"] == "image/png"
    assert downloaded.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert (await api.client.get(url, headers=api.other)).status_code == 404
    selected = await send_message(
        api, second, "改成夜晚", mode="image", generated_image_id=first["generated_image"]["id"]
    )
    response_data(selected, 202)
    assert await run_once(sessions)
    assert provider.await_count == 3
    selected_prompt = provider.await_args.args[0]
    assert "改成夜晚" in selected_prompt
    assert "换成蓝天" not in selected_prompt
    assert provider.await_args.kwargs["reference"] == original


@pytest.mark.parametrize("delete_output_owner", [True, False])
async def test_shared_asset_survives_deleting_one_conversation(
    api, sessions, monkeypatch, configured, delete_output_owner
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    asset_id = first["generated_image"]["id"]
    second = response_data(await create_conversation(api, {
        "message": "蓝天", "mode": "image", "asset_id": asset_id,
    }), 202)
    assert await run_once(sessions)
    target = first["id"] if delete_output_owner else second["id"]
    response_data(await api.client.delete(f"/api/prompt-conversations/{target}", headers=api.owner), 204)
    assert (await api.client.get(f"/api/prompt-assets/{asset_id}", headers=api.owner)).status_code == 200


async def test_signing_failure_preserves_conversation_with_unavailable_image(
    api, sessions, monkeypatch, configured
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "猫", "mode": "image"}), 202)
    assert await run_once(sessions)

    def fail_signing(_):
        raise RuntimeError("private provider details")

    monkeypatch.setattr(storage, "preview_url", fail_signing)
    data = response_data(await api.client.get(f"/api/prompt-conversations/{first['id']}", headers=api.owner))
    turn = data["turns"][0]
    assert turn["response_type"] == "image" and turn["generated_image"] is None
    assert turn["image_status"] == "temporarily_unavailable" and turn["result"] is None


async def test_generated_png_remains_lossless_and_preserves_alpha(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 12), (5, 10, 15, 20)).save(buffer, format="PNG")
    original = buffer.getvalue()
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=original))
    turn = response_data(await create_conversation(api, {"message": "透明的花", "mode": "image"}), 202)
    assert await run_once(sessions)
    turn = response_data(await api.client.get("/api/prompt-analyses/" + turn["id"], headers=api.owner))
    url = f"/api/prompt-conversations/{turn['id']}/images/{turn['generated_image']['id']}/download"
    downloaded = await api.client.get(url, headers=api.owner)
    assert downloaded.content == original
    with Image.open(io.BytesIO(downloaded.content)) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 20


async def test_unconfigured_storage_rejects_before_provider_or_queue(api, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    provider = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", provider)

    def unavailable():
        raise AnalysisError(503, "STORAGE_UNAVAILABLE", "图片存储暂不可用")

    monkeypatch.setattr(storage, "require_storage_configuration", unavailable)
    response = await create_conversation(api, {"message": "猫", "mode": "image"})
    assert response_data(response, 503)["code"] == "STORAGE_UNAVAILABLE"
    provider.assert_not_awaited()


async def test_worker_rechecks_storage_before_provider(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    turn = response_data(await create_conversation(api, {"message": "猫", "mode": "image"}), 202)
    provider = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", provider)

    def unavailable():
        raise AnalysisError(503, "STORAGE_UNAVAILABLE", "图片存储暂不可用")

    monkeypatch.setattr(storage, "require_storage_configuration", unavailable)
    assert await run_once(sessions)
    provider.assert_not_awaited()
    async with sessions() as db:
        job = await db.scalar(select(PromptAnalysis).where(PromptAnalysis.id == turn["id"]))
        assert job.status == "failed" and job.quota_state == "released"


async def test_generated_image_waits_briefly_for_in_process_decoder(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    provider_called = asyncio.Event()

    async def generate(*args, **kwargs):
        provider_called.set()
        return image_bytes()

    monkeypatch.setattr(image_adapter, "generate", generate)
    turn = response_data(await create_conversation(api, {"message": "猫", "mode": "image"}), 202)
    await assets._decode_lock.acquire()
    try:
        worker = asyncio.create_task(run_once(sessions))
        await asyncio.wait_for(provider_called.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not worker.done()
    finally:
        assets._decode_lock.release()
    assert await asyncio.wait_for(worker, timeout=5)
    turn = response_data(await api.client.get("/api/prompt-analyses/" + turn["id"], headers=api.owner))
    assert turn["response_type"] == "image" and turn["generated_image"]


async def test_migration_round_trip_and_refuses_downgrade_with_images(api, sessions, monkeypatch, configured):
    migration = runpy.run_path(str(
        Path(__file__).parents[1] / "alembic/versions/e81d4a9b216f_conversation_generated_images.py"
    ))

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            migration[direction]()

    async with sessions() as db:
        connection = await db.connection()
        await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
        await connection.run_sync(lambda conn: migrate(conn, "upgrade"))
        await db.commit()
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    response_data(await create_conversation(api, {"message": "猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    async with sessions() as db:
        connection = await db.connection()
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await connection.run_sync(lambda conn: migrate(conn, "downgrade"))


async def test_image_retention_and_conversation_deletion(api, sessions, monkeypatch, configured, objects):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    turn = response_data(await create_conversation(api, {"message": "一只猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    turn = response_data(await api.client.get("/api/prompt-analyses/" + turn["id"], headers=api.owner))
    async with sessions() as db, db.begin():
        await db.execute(update(PromptAnalysisAsset).values(
            unbound_expires_at=datetime.now(UTC) - timedelta(days=2)
        ))
    assert await cleanup_assets(sessions) == 0
    assert len(objects) == 2
    async with sessions() as db, db.begin():
        await db.execute(update(PromptAnalysisAsset).values(
            unbound_expires_at=datetime.now(UTC) + timedelta(days=1)
        ))
    response_data(await api.client.delete(f"/api/prompt-conversations/{turn['id']}", headers=api.owner), 204)
    assert (await api.client.get(
        f"/api/prompt-assets/{turn['generated_image']['id']}", headers=api.owner
    )).status_code == 404
    assert await cleanup_assets(sessions) == 1
    assert not objects


async def test_deleted_during_generation_cannot_restore_result(
    api, sessions, monkeypatch, configured, objects
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return image_bytes()

    monkeypatch.setattr(image_adapter, "generate", generate)
    turn = response_data(await create_conversation(api, {"message": "一只猫", "mode": "image"}), 202)
    task = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), timeout=5)
    response_data(await api.client.delete(f"/api/prompt-conversations/{turn['id']}", headers=api.owner), 204)
    release.set()
    assert await task
    async with sessions() as db, db.begin():
        job = await db.scalar(select(PromptAnalysis).where(PromptAnalysis.id == turn["id"]))
        assert job.deleted_at and job.generated_result is None and job.output_asset_id is None
        await db.execute(update(PromptAnalysisAsset).values(
            unbound_expires_at=datetime.now(UTC) - timedelta(days=2)
        ))
    assert await cleanup_assets(sessions) == 1
    assert not objects


async def test_output_binding_is_serialized_with_conversation_deletion(
    api, sessions, monkeypatch, configured, objects
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("test-only"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return image_bytes()

    monkeypatch.setattr(image_adapter, "generate", generate)
    turn = response_data(await create_conversation(api, {"message": "猫", "mode": "image"}), 202)
    worker = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), timeout=5)
    async with sessions() as blocker:
        await budget_lock(blocker)
        deletion = asyncio.create_task(api.client.delete(
            f"/api/prompt-conversations/{turn['id']}", headers=api.owner
        ))
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.sleep(0.1)
        await blocker.commit()
        response_data(await asyncio.wait_for(deletion, timeout=5), 204)
    assert await asyncio.wait_for(worker, timeout=5)
    async with sessions() as db:
        job = await db.scalar(select(PromptAnalysis).where(PromptAnalysis.id == turn["id"]))
        assert job.deleted_at and job.output_asset_id is None and job.generated_result is None
    assert await cleanup_assets(sessions) == 1
    assert not objects
