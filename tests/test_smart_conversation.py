"""Unified chat/image routing with durable jobs and a mocked provider."""

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.services.vision import image_adapter
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import image_bytes, response_data
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_casual_chat import provider, reply
from tests.test_prompt_conversations import create_conversation, send_message

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis URLs required",
    ),
]


async def test_smart_reply_does_not_call_image_provider(api, sessions, monkeypatch, configured):
    calls = provider(monkeypatch, [reply("你好！")])
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "你好", "mode": "smart"}), 202)
    assert first["generation_mode"] is None
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert done["response_type"] == "assistant" and done["assistant_reply"] == "你好！"
    assert len(calls) == 1
    generated.assert_not_awaited()


async def test_smart_image_then_selected_image_edit(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    calls = provider(
        monkeypatch, [{"schema_version": "image-intent-1", "use_reference": False, "use_context": False}]
    )
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    assert first["generation_mode"] is None
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert first["response_type"] == "image" and first["generated_image"]
    assert generated.await_count == 1 and len(calls) == 1
    second = response_data(
        await send_message(
            api, first, "换成夜景", mode="smart", generated_image_id=first["generated_image"]["id"]
        ),
        202,
    )
    assert await run_once(sessions)
    second = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert second["response_type"] == "image"
    assert generated.await_count == 2 and generated.await_args.kwargs["reference"] is not None


async def test_smart_rejects_unowned_image_reference(api, sessions, monkeypatch, configured):
    from uuid import uuid4

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(monkeypatch, [reply()])
    first = response_data(await create_conversation(api, {"message": "你好", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    response_data(await send_message(api, first, "编辑", mode="smart", generated_image_id=str(uuid4())), 404)


async def test_smart_implicit_reference_not_reported_as_user_upload(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(
        monkeypatch,
        [
            {"schema_version": "image-intent-1", "use_reference": False, "use_context": False},
            {"schema_version": "image-intent-1", "use_reference": True, "use_context": False},
        ],
    )
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    second = response_data(await send_message(api, first, "换成夜景", mode="smart"), 202)
    assert second["asset_id"] is None
    assert await run_once(sessions)
    assert generated.await_args.kwargs["reference"] == image_bytes()


async def test_unavailable_previous_image_does_not_break_chat_or_silently_generate(
    api, sessions, monkeypatch, configured
):
    from app.services.prompt_analysis import storage

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(
        monkeypatch,
        [
            {"schema_version": "image-intent-1", "use_reference": False, "use_context": False},
            reply("你好！"),
            {"schema_version": "image-intent-1", "use_reference": True, "use_context": False},
        ],
    )
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    failed_read = AsyncMock(side_effect=OSError("unavailable"))
    monkeypatch.setattr(storage, "read_generated", failed_read)
    second = response_data(await send_message(api, first, "你好", mode="smart"), 202)
    assert await run_once(sessions)
    second = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert second["response_type"] == "assistant"
    failed_read.assert_not_awaited()
    third = response_data(await send_message(api, second, "把刚才的图换成夜景", mode="smart"), 202)
    assert await run_once(sessions)
    third = response_data(await api.client.get("/api/prompt-analyses/" + third["id"], headers=api.owner))
    assert third["status"] == "failed" and third["error_code"] == "image_unavailable"
    assert generated.await_count == 1


async def test_question_about_previous_image_loads_pixels_before_answering(
    api, sessions, monkeypatch, configured
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    calls = provider(
        monkeypatch,
        [
            {"schema_version": "image-intent-1", "use_reference": False, "use_context": False},
            {"schema_version": "image-question-1"},
            reply("画面里有一只猫。"),
        ],
    )
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    second = response_data(await send_message(api, first, "刚才那张图片里是什么？", mode="smart"), 202)
    assert await run_once(sessions)
    second = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert second["assistant_reply"] == "画面里有一只猫。"
    classified = json.loads(calls[1]["messages"][1]["content"])
    assert classified["context"]["has_generated_image"] is True
    assert calls[2]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_delete_during_classification_prevents_image_dispatch(api, sessions, monkeypatch, configured):
    from app.schemas.prompt_conversation import ConversationImageIntent
    from app.services import vision

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def decide(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return vision.VisionResult(
            ConversationImageIntent(schema_version="image-intent-1", use_reference=False, use_context=False),
            None,
            {},
        )

    monkeypatch.setattr(vision, "generate_conversation", decide)
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    task = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), 3)
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    proceed.set()
    assert await asyncio.wait_for(task, 3)
    generated.assert_not_awaited()


async def test_smart_image_only_upload_uses_reference(api, sessions, monkeypatch, configured):
    from tests.test_prompt_api_integration import upload

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(monkeypatch, [{"schema_version": "image-intent-1", "use_reference": True, "use_context": False}])
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    reference = await upload(api)
    first = response_data(await create_conversation(api, {"asset_id": reference["id"], "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert first["response_type"] == "image"
    assert generated.await_args.kwargs["reference"] is not None


async def test_new_unrelated_image_does_not_automatically_edit_last(api, sessions, monkeypatch, configured):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(
        monkeypatch, [{"schema_version": "image-intent-1", "use_reference": False, "use_context": False}]
    )
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    response_data(await send_message(api, first, "再画一幅无关的山水画", mode="smart"), 202)
    assert await run_once(sessions)
    assert generated.await_args.kwargs["reference"] is None
    assert "画一只猫" not in generated.await_args.args[0]


async def test_smart_request_can_generate_from_prior_prompt(api, sessions, monkeypatch, configured):
    from tests.test_prompt_vision import valid_result

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    prompt = valid_result()
    provider(
        monkeypatch,
        [prompt, {"schema_version": "image-intent-1", "use_reference": False, "use_context": True}],
    )
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "写猫的提示词", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert first["response_type"] == "prompt"
    response_data(await send_message(api, first, "根据上面的提示词画图", mode="smart"), 202)
    assert await run_once(sessions)
    assert prompt["prompt"]["zh"] in generated.await_args.args[0]


async def test_delete_during_image_question_prevents_second_vision_call(
    api, sessions, monkeypatch, configured
):
    from app.schemas.prompt_conversation import ConversationImageQuestion
    from app.services import vision

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = []

    async def decide(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            await proceed.wait()
            return vision.VisionResult(ConversationImageQuestion(schema_version="image-question-1"), None, {})
        raise AssertionError("Must not call vision after deletion")

    monkeypatch.setattr(vision, "generate_conversation", decide)
    response_data(
        await send_message(
            api, first, "这张图里有什么？", mode="smart", generated_image_id=first["generated_image"]["id"]
        ),
        202,
    )
    task = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), 3)
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    proceed.set()
    assert await asyncio.wait_for(task, 3)
    assert len(calls) == 1


async def test_delete_during_image_read_prevents_second_vision_call(api, sessions, monkeypatch, configured):
    from app.schemas.prompt_conversation import ConversationImageQuestion
    from app.services import vision
    from app.services.prompt_analysis import storage

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    calls = []

    async def decide(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            raise AssertionError("Must not call vision after deletion")
        return vision.VisionResult(ConversationImageQuestion(schema_version="image-question-1"), None, {})

    entered, proceed = asyncio.Event(), asyncio.Event()

    async def delayed_read(_key):
        entered.set()
        await proceed.wait()
        return image_bytes()

    monkeypatch.setattr(vision, "generate_conversation", decide)
    monkeypatch.setattr(storage, "read_generated", delayed_read)
    response_data(await send_message(api, first, "这张图是什么？", mode="smart"), 202)
    task = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), 3)
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    proceed.set()
    assert await asyncio.wait_for(task, 3)
    assert len(calls) == 1
