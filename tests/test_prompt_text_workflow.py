"""Whole private text workflow: real SQL/Redis, real SDK over a local HTTP transport."""

import asyncio
import json
import os
import uuid

import pytest

from app.services.prompt_analysis import storage
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import (
    api as api,
)
from tests.test_prompt_api_integration import (
    configured as configured,
)
from tests.test_prompt_api_integration import (
    limiter as limiter,
)
from tests.test_prompt_api_integration import (
    objects as objects,
)
from tests.test_prompt_api_integration import (
    response_data,
)
from tests.test_prompt_api_integration import (
    sessions as sessions,
)
from tests.test_prompt_vision import completion_response, install_transport, valid_result

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis test URLs required",
    ),
]


async def test_text_http_worker_bilingual_edit_history_delete(api, sessions, monkeypatch):
    calls = []

    async def reject_storage(*args):
        raise AssertionError("Text tasks must never read or sign a private image")

    monkeypatch.setattr(storage, "read_private", reject_storage)
    monkeypatch.setattr(storage, "preview_url", reject_storage)

    def upstream(request):
        data = json.loads(request.content)
        calls.append(data)
        assert isinstance(data["messages"][1]["content"], str)
        assert json.loads(data["messages"][1]["content"])["idea_text"] == "雨夜送花的小猫"
        return completion_response(json.dumps(valid_result(), ensure_ascii=False))

    install_transport(monkeypatch, upstream)
    key = str(uuid.uuid4())
    headers = {**api.owner, "Idempotency-Key": key}
    payload = {"input_type": "text", "idea_text": "雨夜送花的小猫",
               "purpose": "wallpaper", "style": "cinematic"}
    created = response_data(await api.client.post("/api/prompt-analyses", json=payload, headers=headers), 202)
    assert created["input_type"] == "text" and created["asset_id"] is None
    assert created["status"] == "queued" and created["saved_to_history"] is False
    assert await run_once(sessions)
    assert not await run_once(sessions)
    path = "/api/prompt-analyses/" + created["id"]
    completed = response_data(await api.client.get(path, headers=api.owner))
    assert completed["status"] == "succeeded" and completed["saved_to_history"] is True
    assert completed["prompt"] == valid_result()["prompt"]
    assert completed["idea_text"] == payload["idea_text"]
    assert completed["image_available"] is False and completed["preview_url"] is None
    edited_prompt = {**completed["prompt"], "zh": "用户手动修改的中文"}
    edited = response_data(await api.client.patch(
        path, headers=api.owner, json={"revision": completed["revision"], "prompt": edited_prompt}
    ))
    assert edited["prompt"]["zh"] == "用户手动修改的中文"
    assert edited["prompt"]["en"] == completed["prompt"]["en"]
    assert edited["revision"] == 1
    history = response_data(await api.client.get("/api/prompt-analyses", headers=api.owner))
    assert history["items"][0]["id"] == created["id"]
    assert history["items"][0]["idea_text"] == payload["idea_text"]
    assert history["items"][0]["summary"]["zh"] == "用户手动修改的中文"
    assert len(calls) == 1
    response_data(await api.client.get(path, headers=api.other), 404)
    response_data(await api.client.delete(path, headers=api.owner), 204)
    response_data(await api.client.get(path, headers=api.owner), 404)
    response_data(await api.client.post("/api/prompt-analyses", json=payload, headers=headers), 404)
    assert len(calls) == 1


async def test_deleted_running_text_never_restores_input_or_result(api, sessions, monkeypatch):
    from app.models import PromptAnalysis

    started, release = asyncio.Event(), asyncio.Event()

    async def upstream(request):
        started.set()
        await release.wait()
        return completion_response(json.dumps(valid_result(), ensure_ascii=False))

    install_transport(monkeypatch, upstream)
    headers = {**api.owner, "Idempotency-Key": str(uuid.uuid4())}
    payload = {"input_type": "text", "idea_text": "必须被删除的私人想法"}
    created = response_data(await api.client.post("/api/prompt-analyses", json=payload, headers=headers), 202)
    path = "/api/prompt-analyses/" + created["id"]
    task = asyncio.create_task(run_once(sessions))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        response_data(await api.client.delete(path, headers=api.owner), 204)
    finally:
        release.set()
        await task
    response_data(await api.client.get(path, headers=api.owner), 404)
    async with sessions() as db:
        tombstone = await db.get(PromptAnalysis, uuid.UUID(created["id"]))
        assert tombstone.deleted_at is not None
        assert tombstone.idea_text is None and tombstone.generated_result is None
        assert tombstone.edited_prompts is None and tombstone.quota_state == "consumed"
    response_data(await api.client.post("/api/prompt-analyses", json=payload, headers=headers), 404)
