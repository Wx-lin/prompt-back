"""Agent and legacy tasks retain the same durable worker safety boundaries."""

import os
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from app.models import PromptAnalysis
from app.schemas.prompt_conversation import (
    ConversationImageIntent,
    ConversationImageQuestion,
    ConversationReplyResult,
)
from app.services import vision
from app.services.vision import image_adapter
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import image_bytes, response_data
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation, send_message

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis URLs required",
    ),
]


async def test_new_smart_task_uses_agent_without_generating_image_for_capability_question(
    api, sessions, monkeypatch, configured,
):
    agent = ModuleType("app.services.vision.agent")
    agent.generate_conversation = AsyncMock(return_value=vision.VisionResult(
        ConversationReplyResult(schema_version="reply-1", title="生图", reply="可以，请描述画面。"), None, {},
    ))
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    legacy = AsyncMock(side_effect=AssertionError("new smart task must use agent"))
    monkeypatch.setattr(vision, "generate_conversation", legacy)
    generate = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", generate)

    task = response_data(await create_conversation(api, {"message": "你能生图吗", "mode": "smart"}), 202)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + task["id"], headers=api.owner))
    assert done["assistant_reply"] == "可以，请描述画面。"
    agent.generate_conversation.assert_awaited_once()
    legacy.assert_not_awaited()
    generate.assert_not_awaited()
    async with sessions() as db:
        import uuid
        row = await db.get(PromptAnalysis, uuid.UUID(task["id"]))
        assert row.prompt_version == "agent-v1"
        assert row.config_snapshot["prompt_version"] == "agent-v1"


async def test_legacy_queued_smart_task_stays_on_legacy_handler(api, sessions, monkeypatch, configured):
    import uuid

    task = response_data(await create_conversation(api, {"message": "你好", "mode": "smart"}), 202)
    async with sessions() as db, db.begin():
        row = await db.get(PromptAnalysis, uuid.UUID(task["id"]))
        row.prompt_version = "chat-v2"
        row.config_snapshot = {**row.config_snapshot, "prompt_version": "chat-v2"}
    legacy = AsyncMock(return_value=vision.VisionResult(
        ConversationReplyResult(schema_version="reply-1", title="聊天", reply="你好。"), None, {},
    ))
    monkeypatch.setattr(vision, "generate_conversation", legacy)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + task["id"], headers=api.owner))
    assert done["status"] == "succeeded" and done["assistant_reply"] == "你好。"
    assert not await run_once(sessions)
    legacy.assert_awaited_once()


@pytest.mark.parametrize(("mode", "version"), [
    ("smart", "chat-v2"), ("smart", "agent-v1"), ("image", "image-1"),
])
async def test_explicit_retry_freezes_original_version(api, sessions, monkeypatch, configured, mode, version):
    import uuid

    from pydantic import SecretStr

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    reply = vision.VisionResult(
        ConversationReplyResult(schema_version="reply-1", title="聊天", reply="你好。"), None, {},
    )
    legacy = AsyncMock(side_effect=[vision.VisionError("model_upstream_error"), reply])
    monkeypatch.setattr(vision, "generate_conversation", legacy)
    agent = ModuleType("app.services.vision.agent")
    agent.generate_conversation = AsyncMock(side_effect=[vision.VisionError("model_upstream_error"), reply])
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    image_generate = AsyncMock(side_effect=[vision.VisionError("model_upstream_error"), image_bytes()])
    monkeypatch.setattr(image_adapter, "generate", image_generate)
    first = response_data(await create_conversation(api, {"message": "你好", "mode": mode}), 202)
    async with sessions() as db, db.begin():
        row = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        row.prompt_version = version
        row.config_snapshot = {**row.config_snapshot, "prompt_version": version}
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert first["status"] == "failed"
    retry = response_data(await api.client.post(
        "/api/prompt-conversations/" + first["id"] + "/retry",
        headers={**api.owner, "Idempotency-Key": str(uuid.uuid4())}, json={"analysis_id": first["id"]},
    ), 202)
    async with sessions() as db:
        row = await db.get(PromptAnalysis, uuid.UUID(retry["id"]))
        assert row.prompt_version == version and row.config_snapshot["prompt_version"] == version
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + retry["id"], headers=api.owner))
    assert done["status"] == "succeeded"
    assert (legacy.await_count, agent.generate_conversation.await_count, image_generate.await_count) == (
        (2, 0, 0) if version == "chat-v2" else (0, 2, 0) if version == "agent-v1" else (0, 0, 2)
    )


@pytest.mark.parametrize(("mode", "version", "snapshot"), [
    ("smart", "agent-v2", "agent-v2"),
    ("auto", "agent-v1", "agent-v1"),
    ("smart", "agent-v1", "chat-v2"),
])
async def test_unrecognized_or_mismatched_versions_never_dispatch(
    api, sessions, monkeypatch, configured, mode, version, snapshot,
):
    import uuid

    task = response_data(await create_conversation(api, {"message": "你好", "mode": mode}), 202)
    async with sessions() as db, db.begin():
        row = await db.get(PromptAnalysis, uuid.UUID(task["id"]))
        row.prompt_version = version
        row.config_snapshot = {**row.config_snapshot, "prompt_version": snapshot}
    legacy = AsyncMock()
    monkeypatch.setattr(vision, "generate_conversation", legacy)
    generate = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", generate)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + task["id"], headers=api.owner))
    assert done["status"] == "failed" and done["error_code"] == "configuration_changed"
    legacy.assert_not_awaited()
    generate.assert_not_awaited()


@pytest.mark.parametrize("invalid", [False, True])
async def test_agent_image_question_second_call_only_accepts_text(
    api, sessions, monkeypatch, configured, invalid,
):
    from pydantic import SecretStr

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    generate = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generate)
    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    agent = ModuleType("app.services.vision.agent")
    last = (
        ConversationImageIntent(schema_version="image-intent-1", use_reference=True, use_context=True)
        if invalid else ConversationReplyResult(schema_version="reply-1", title="图片", reply="画中有猫。")
    )
    agent.generate_conversation = AsyncMock(side_effect=[
        vision.VisionResult(ConversationImageQuestion(schema_version="image-question-1"), "first", {
            "prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
            "input_tokens": 10, "output_tokens": 3,
            "reasoning_tokens": 2, "image_tokens": True, "provider_body": "private",
        }),
        vision.VisionResult(last, "second", {
            "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25,
            "reasoning_tokens": 1, "image_tokens": -1,
        }),
    ])
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    legacy = AsyncMock(side_effect=AssertionError("agent question must not fall back to legacy"))
    monkeypatch.setattr(vision, "generate_conversation", legacy)
    second = response_data(await send_message(api, first, "刚才的图片是什么？", mode="smart"), 202)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert agent.generate_conversation.await_count == 2
    assert agent.generate_conversation.await_args.kwargs["image"] == image_bytes()
    assert agent.generate_conversation.await_args.kwargs["mode"] == "smart"
    assert done["status"] == ("failed" if invalid else "succeeded")
    if not invalid:
        assert done["assistant_reply"] == "画中有猫。"
        import uuid

        from sqlalchemy import select

        from app.models import PromptAnalysisAttempt
        async with sessions() as db:
            attempt = await db.scalar(select(PromptAnalysisAttempt).where(
                PromptAnalysisAttempt.analysis_id == uuid.UUID(second["id"]),
            ))
            assert attempt.usage == {
                "prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38, "reasoning_tokens": 3,
            }
            assert attempt.provider_request_id == "second"
    legacy.assert_not_awaited()
    assert generate.await_count == 1


async def test_agent_image_intent_still_requires_fenced_dispatch(api, sessions, monkeypatch, configured):
    import asyncio

    from pydantic import SecretStr

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    entered, proceed = asyncio.Event(), asyncio.Event()
    agent = ModuleType("app.services.vision.agent")

    async def decide(*_args, **_kwargs):
        entered.set()
        await proceed.wait()
        return vision.VisionResult(
            ConversationImageIntent(schema_version="image-intent-1", use_reference=False, use_context=False),
            None, {},
        )

    agent.generate_conversation = decide
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    generate = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", generate)
    task = response_data(await create_conversation(api, {"message": "画一只猫", "mode": "smart"}), 202)
    running = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), 3)
    response_data(await api.client.delete("/api/prompt-conversations/" + task["id"], headers=api.owner), 204)
    proceed.set()
    assert await asyncio.wait_for(running, 3)
    generate.assert_not_awaited()


async def test_agent_stops_followup_model_calls_when_conversation_deleted(
    api, sessions, monkeypatch, configured,
):
    import asyncio

    entered, resume = asyncio.Event(), asyncio.Event()
    model_calls = []
    agent = ModuleType("app.services.vision.agent")

    async def run_agent(*_args, before_model_call, **_kwargs):
        if await before_model_call():
            model_calls.append(1)
        entered.set()
        await resume.wait()
        if await before_model_call():
            model_calls.append(2)
        return vision.VisionResult(
            ConversationReplyResult(schema_version="reply-1", title="聊天", reply="你好"), None, {},
        )

    agent.generate_conversation = run_agent
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    generated = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "你好", "mode": "smart"}), 202)
    running = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), 3)
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    resume.set()
    assert await asyncio.wait_for(running, 3)
    assert model_calls == [1]
    generated.assert_not_awaited()


async def test_agent_shares_model_call_budget_with_image_followup(api, sessions, monkeypatch, configured):
    from pydantic import SecretStr

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    outcomes = []
    agent = ModuleType("app.services.vision.agent")

    async def run_agent(*_args, before_model_call, image, **_kwargs):
        outcomes.extend([await before_model_call(), await before_model_call()])
        result = (
            ConversationReplyResult(schema_version="reply-1", title="图片", reply="画中有猫")
            if image else ConversationImageQuestion(schema_version="image-question-1")
        )
        return vision.VisionResult(result, None, {})

    agent.generate_conversation = run_agent
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    second = response_data(await send_message(api, first, "刚才图片里有什么？", mode="smart"), 202)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert done["assistant_reply"] == "画中有猫"
    assert outcomes == [True, True, True, False]
    assert generated.await_count == 1


async def test_real_agent_loop_checks_deleted_conversation_before_second_model_call(
    api, sessions, monkeypatch, configured,
):
    import asyncio

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from pydantic import Field

    from app.services.vision import agent

    entered, resume = asyncio.Event(), asyncio.Event()

    class PausedModel(FakeMessagesListChatModel):
        calls: list = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            return self

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                entered.set()
                await resume.wait()
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = PausedModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "get_creation_capabilities", "args": {}, "id": "call-1", "type": "tool_call",
        }]),
        AIMessage(content="可以生图"),
    ])
    monkeypatch.setattr(agent, "_make_model", lambda *_args: model)
    monkeypatch.setattr(agent.adapter, "require_vision_configuration", lambda: None)
    monkeypatch.setattr(agent, "_image_available", lambda: True)
    generated = AsyncMock()
    monkeypatch.setattr(image_adapter, "generate", generated)
    first = response_data(await create_conversation(api, {"message": "你有什么能力", "mode": "smart"}), 202)
    running = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), 5)
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    resume.set()
    assert await asyncio.wait_for(running, 5)
    assert len(model.calls) == 1
    generated.assert_not_awaited()


async def test_real_agent_answers_capability_then_dispatches_explicit_image_once(
    api, sessions, monkeypatch, configured,
):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from pydantic import SecretStr

    from app.services.vision import agent

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    models = iter([
        ToolModel(responses=[AIMessage(content="可以，请说说你想画什么。")]),
        ToolModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "request_image_generation", "args": {"use_reference": False, "use_context": True},
            "id": "call-image", "type": "tool_call",
        }])]),
    ])
    monkeypatch.setattr(agent, "_make_model", lambda *_args: next(models))
    monkeypatch.setattr(agent.adapter, "require_vision_configuration", lambda: None)
    monkeypatch.setattr(agent, "_image_available", lambda: True)
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    generated = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generated)

    first = response_data(await create_conversation(api, {"message": "你能生图吗？", "mode": "smart"}), 202)
    assert await run_once(sessions)
    first = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert first["response_type"] == "assistant" and "可以" in first["assistant_reply"]
    generated.assert_not_awaited()
    second = response_data(await send_message(api, first, "那画一只猫", mode="smart"), 202)
    assert await run_once(sessions)
    second = response_data(await api.client.get("/api/prompt-analyses/" + second["id"], headers=api.owner))
    assert second["response_type"] == "image"
    assert generated.await_count == 1
