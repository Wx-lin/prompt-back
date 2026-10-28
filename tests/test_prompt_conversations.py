"""Real private conversation API/worker flow, with local provider/COS transports."""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Prompt, PromptAnalysis, PromptImage
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import image_bytes, response_data, upload
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_vision import completion_response, install_transport, valid_result

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis test URLs required",
    ),
]


async def create_conversation(api, body=None, *, key=None, headers=None):
    return await api.client.post(
        "/api/prompt-conversations",
        json=body or {"message": "雨夜送花的小猫"},
        headers={**(headers or api.owner), "Idempotency-Key": key or str(uuid.uuid4())},
    )


async def send_message(api, parent, message="改成白天，保留构图", **extra):
    return await api.client.post(
        f"/api/prompt-conversations/{parent['conversation_id']}/messages",
        headers={**api.owner, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "message": message,
            "parent_analysis_id": parent["id"],
            "parent_revision": parent["revision"],
            **extra,
        },
    )


@pytest.mark.parametrize(
    "body",
    [{}, {"message": " "}, {"message": 4}, {"message": "猫" * 501}, {"message": "猫", "unexpected": True}],
)
async def test_conversation_rejects_invalid_inputs(api, body):
    response = await api.client.post(
        "/api/prompt-conversations",
        headers={**api.owner, "Idempotency-Key": str(uuid.uuid4())},
        json=body,
    )
    assert response_data(response, 422)["code"] == "INVALID_REQUEST"


async def test_conversation_authentication_is_private(api):
    response_data(await api.client.post("/api/prompt-conversations", json={"message": "猫"}), 401)
    response_data(await api.client.get("/api/prompt-conversations"), 401)


async def test_text_conversation_preserves_effective_context_versions_and_history(api, sessions, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(valid_result(), ensure_ascii=False))

    install_transport(monkeypatch, upstream)
    first = response_data(await create_conversation(api), 202)
    assert first["conversation_id"] == first["id"] and first["version"] == 1
    assert first["parent_analysis_id"] is None and first["user_message"] == "雨夜送花的小猫"
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    changed = {**first["prompt"], "zh": "用户编辑后的小猫提示词"}
    first = response_data(
        await api.client.patch(
            "/api/prompt-analyses/" + first["id"],
            headers=api.owner,
            json={"revision": first["revision"], "prompt": changed},
        )
    )
    second = response_data(await send_message(api, first), 202)
    assert second["version"] == 2 and second["conversation_id"] == first["id"]
    assert second["parent_analysis_id"] == first["id"]
    assert await run_once(sessions)
    assert not await run_once(sessions)
    snapshot = calls[1]["messages"][1]["content"]
    assert "用户编辑后的小猫提示词" in str(snapshot)
    assert "改成白天，保留构图" in str(snapshot)
    assert len(calls) == 2
    conv = response_data(
        await api.client.get(
            "/api/prompt-conversations/" + first["id"],
            headers=api.owner,
        )
    )
    assert [item["version"] for item in conv["turns"]] == [1, 2]
    assert conv["turns"][0]["prompt"] == changed
    listing = response_data(await api.client.get("/api/prompt-conversations", headers=api.owner))
    assert listing["items"][0]["id"] == first["id"]
    assert listing["items"][0]["latest_analysis_id"] == second["id"]
    assert listing["items"][0]["version"] == 2
    assert len(calls) == 2
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    response_data(await api.client.get("/api/prompt-conversations/" + first["id"], headers=api.owner), 404)
    async with sessions() as db:
        for job in await db.scalars(select(PromptAnalysis)):
            assert job.deleted_at is not None
            assert job.user_message is None and job.context_snapshot is None
            assert job.generated_result is None and job.idea_text is None


async def test_parent_ownership_revision_and_latest_version_are_enforced(api, sessions, monkeypatch):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    path = "/api/prompt-conversations/" + first["id"]
    response_data(await api.client.get(path, headers=api.other), 404)
    response_data(await api.client.delete(path, headers=api.other), 404)
    response_data(
        await api.client.post(
            path + "/messages",
            headers={
                **api.other,
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={"message": "偷看", "parent_analysis_id": first["id"], "parent_revision": 0},
        ),
        404,
    )
    error = response_data(await send_message(api, first, parent_revision=3), 409)
    assert error["code"] == "PARENT_REVISION_CONFLICT"
    second = response_data(await send_message(api, first), 202)
    assert await run_once(sessions)
    error = response_data(await send_message(api, first), 409)
    assert error["code"] == "CONVERSATION_CHANGED"
    response_data(await send_message(api, second), 202)


async def test_conversation_replay_survives_configuration_and_parent_changes(
    api, sessions, configured, monkeypatch
):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    key = str(uuid.uuid4())
    first = response_data(await create_conversation(api, key=key), 202)
    assert await run_once(sessions)
    response_data(await send_message(api, first), 202)
    configured.prompt_analysis_enabled = False
    replay = response_data(await create_conversation(api, key=key))
    assert replay["id"] == first["id"]
    error = response_data(await create_conversation(api, {"message": "换成小狗"}, key=key), 409)
    assert error["code"] == "IDEMPOTENCY_CONFLICT"


async def test_image_message_is_an_actual_multimodal_prompt_and_can_refine_without_image(
    api, sessions, monkeypatch
):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(valid_result()))

    install_transport(monkeypatch, upstream)
    asset = await upload(api)
    first = response_data(
        await create_conversation(
            api,
            {
                "asset_id": asset["id"],
                "message": "参考图改成奶油白背景",
            },
        ),
        202,
    )
    assert first["input_type"] == "image" and first["user_message"] == "参考图改成奶油白背景"
    assert await run_once(sessions)
    content = calls[0]["messages"][1]["content"]
    assert any(part.get("type") == "image_url" for part in content)
    assert "奶油白" in str(content)
    response_data(await send_message(api, first, "主体不变，换成夜景"), 202)
    assert await run_once(sessions)
    assert "image_url" not in str(calls[1]["messages"][1]["content"])
    assert "夜景" in str(calls[1])


async def test_legacy_analysis_is_a_single_conversation_with_string_metadata(api, sessions, monkeypatch):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    response = await api.client.post(
        "/api/prompt-analyses",
        headers={
            **api.owner,
            "Idempotency-Key": str(uuid.uuid4()),
        },
        json={"input_type": "text", "idea_text": "老任务"},
    )
    first = response_data(response, 202)
    assert first["conversation_id"] == first["id"]
    assert first["user_message"] == "老任务" and first["version"] == 1
    assert await run_once(sessions)
    conv = response_data(await api.client.get("/api/prompt-conversations/" + first["id"], headers=api.owner))
    assert len(conv["turns"]) == 1
    response_data(await send_message(api, first), 202)


async def test_conversation_privacy_data_expires_and_delete_keeps_quota(api, sessions, monkeypatch):
    from app.services.prompt_analysis.cleanup import expire_history

    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    second = response_data(await send_message(api, first), 202)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        job.history_retain_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await expire_history(sessions) == 1
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        assert job.user_message is None and job.context_snapshot is None
        assert job.idea_text is None


async def seed_public_prompt(sessions, url, **extra):
    async with sessions() as db:
        prompt = Prompt(
            title="公开参考图",
            prompt_text="作品原有提示词",
            category="product",
            models=[],
            is_published=True,
            **extra,
        )
        db.add(prompt)
        await db.flush()
        db.add(PromptImage(prompt_id=prompt.id, image_url=url, sort_order=0))
        await db.commit()
        return str(prompt.id)


async def test_from_prompt_only_reads_public_owned_cos_objects(api, sessions, configured, monkeypatch):
    from app.services.prompt_analysis import storage

    configured.cos_bucket = "test-bucket"
    configured.cos_region = "ap-guangzhou"
    prompt_id = await seed_public_prompt(
        sessions, "https://test-bucket.cos.ap-guangzhou.myqcloud.com/prompts/public-test.png"
    )
    calls = []

    async def read(key):
        calls.append(key)
        return image_bytes(), "image/png"

    monkeypatch.setattr(storage, "read_public_prompt", read, raising=False)
    result = response_data(
        await api.client.post(
            "/api/prompt-assets/from-prompt", headers=api.owner, json={"prompt_id": prompt_id}
        ),
        201,
    )
    assert result["id"] and result["preview_url"]
    assert calls == ["prompts/public-test.png"]
    external = await seed_public_prompt(sessions, "https://attacker.invalid/private")
    error = response_data(
        await api.client.post(
            "/api/prompt-assets/from-prompt", headers=api.owner, json={"prompt_id": external}
        ),
        422,
    )
    assert error["code"] == "SOURCE_IMAGE_UNAVAILABLE"
    response_data(
        await api.client.post(
            "/api/prompt-assets/from-prompt",
            headers=api.owner,
            json={"prompt_id": prompt_id, "url": "http://127.0.0.1"},
        ),
        422,
    )
    assert len(calls) == 1
