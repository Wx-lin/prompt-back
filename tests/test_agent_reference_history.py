"""Agent reference history is private, time-bounded, and branch-aware."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import PromptAnalysis, PromptAnalysisAsset
from app.services.prompt_analysis import conversations
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import response_data, upload
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_casual_chat import provider, reply
from tests.test_prompt_conversations import create_conversation, send_message

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis test URLs required",
    ),
]


async def test_agent_context_allows_12_turns_but_limits_each_reply_and_total_history():
    from pydantic import ValidationError

    from app.schemas.prompt_conversation import AgentConversationContext, ConversationContext

    twelve = [{"user": "x", "assistant": "ok"} for _ in range(12)]
    raw = {"title": "test", "recent_messages": ["x"] * 12, "recent_dialogue": twelve}
    assert len(AgentConversationContext.model_validate(raw).recent_dialogue) == 12
    with pytest.raises(ValidationError):
        ConversationContext.model_validate(raw)
    with pytest.raises(ValidationError):
        AgentConversationContext.model_validate(
            {**raw, "recent_dialogue": [{"user": "x", "assistant": "a" * 2001}]}
        )
    with pytest.raises(ValidationError):
        AgentConversationContext.model_validate(
            {**raw, "recent_dialogue": [{"user": "u" * 500, "assistant": "a" * 1500}] * 7}
        )


async def _complete(api, sessions, turn):
    assert await run_once(sessions)
    return response_data(await api.client.get("/api/prompt-analyses/" + turn["id"], headers=api.owner))


async def test_agent_reference_uses_latest_uploaded_image_in_same_user_conversation(
    api, sessions, monkeypatch
):
    provider(monkeypatch, [reply()])
    image_a, image_b = await upload(api), await upload(api)
    first = response_data(await create_conversation(api, {"asset_id": image_a["id"], "message": "看图"}), 202)
    first = await _complete(api, sessions, first)
    second = response_data(await send_message(api, first, "换一张图", asset_id=image_b["id"]), 202)
    await _complete(api, sessions, second)
    async with sessions() as db:
        chosen = await conversations.latest_reference(db, api.user_id, uuid.UUID(first["conversation_id"]))
        assert chosen.id == uuid.UUID(image_b["id"])
        assert (
            await conversations.latest_reference(db, uuid.uuid4(), uuid.UUID(first["conversation_id"]))
            is None
        )


async def test_other_conversation_image_does_not_override_current_conversation(api, sessions, monkeypatch):
    provider(monkeypatch, [reply()])
    image_a, image_b = await upload(api), await upload(api)
    first = response_data(
        await create_conversation(api, {"asset_id": image_a["id"], "message": "会话A"}), 202
    )
    first = await _complete(api, sessions, first)
    other = response_data(
        await create_conversation(api, {"asset_id": image_b["id"], "message": "会话B"}), 202
    )
    await _complete(api, sessions, other)
    async with sessions() as db:
        chosen = await conversations.latest_reference(db, api.user_id, uuid.UUID(first["conversation_id"]))
        assert chosen.id == uuid.UUID(image_a["id"])


async def test_generated_output_is_newest_reference_after_uploaded_input(
    api, sessions, monkeypatch, configured
):
    from unittest.mock import AsyncMock

    from pydantic import SecretStr

    from app.services.vision import image_adapter
    from tests.test_prompt_api_integration import image_bytes

    provider(monkeypatch, [reply()])
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    uploaded = await upload(api)
    first = response_data(
        await create_conversation(api, {"asset_id": uploaded["id"], "message": "分析图"}), 202
    )
    first = await _complete(api, sessions, first)
    second = response_data(await send_message(api, first, "生成一张猫图", mode="image"), 202)
    second = await _complete(api, sessions, second)
    async with sessions() as db:
        chosen = await conversations.latest_reference(db, api.user_id, uuid.UUID(first["conversation_id"]))
        assert chosen.id == uuid.UUID(second["generated_image"]["id"])


async def test_expired_or_deleted_latest_reference_never_falls_back_to_other_image(
    api, sessions, monkeypatch
):
    provider(monkeypatch, [reply()])
    image_a, image_b = await upload(api), await upload(api)
    first = response_data(await create_conversation(api, {"asset_id": image_a["id"], "message": "旧图"}), 202)
    first = await _complete(api, sessions, first)
    second = response_data(await send_message(api, first, "新图", asset_id=image_b["id"]), 202)
    second = await _complete(api, sessions, second)
    async with sessions() as db:
        latest = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        latest.image_retain_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert (
            await conversations.latest_reference(db, api.user_id, uuid.UUID(first["conversation_id"])) is None
        )
        latest.image_retain_until = datetime.now(UTC) + timedelta(hours=1)
        asset = await db.scalar(
            select(PromptAnalysisAsset).where(PromptAnalysisAsset.id == uuid.UUID(image_b["id"]))
        )
        asset.state = "deleted"
        await db.commit()
        assert (
            await conversations.latest_reference(db, api.user_id, uuid.UUID(first["conversation_id"])) is None
        )


@pytest.mark.parametrize("agent_version,has_reference_field", [("agent-v1", True), ("chat-v2", False)])
async def test_only_agent_snapshot_exposes_uploaded_reference_availability(
    api, sessions, monkeypatch, agent_version, has_reference_field
):
    from app.services.prompt_analysis import api as api_service

    provider(monkeypatch, [reply()])
    image = await upload(api)
    first = response_data(await create_conversation(api, {"asset_id": image["id"], "message": "看图"}), 202)
    first = await _complete(api, sessions, first)
    monkeypatch.setattr(api_service, "new_conversation_version", lambda _mode: agent_version, raising=False)
    second = response_data(await send_message(api, first, "把它改明亮", mode="smart"), 202)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        assert ("has_reference_image" in job.context_snapshot) is has_reference_field
        if has_reference_field:
            assert job.context_snapshot["has_reference_image"] is True


async def test_agent_context_preserves_recent_12_turns_with_a_bounded_dialogue_budget(
    api, sessions, monkeypatch
):
    provider(monkeypatch, [reply()])
    first = response_data(await create_conversation(api, {"message": "第1轮"}), 202)
    await _complete(api, sessions, first)
    async with sessions() as db:
        root = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        now = datetime.now(UTC)
        new_turns = []
        for index in range(2, 15):
            turn = PromptAnalysis(
                id=uuid.uuid4(),
                user_id=api.user_id,
                input_type="text",
                idea_text=f"第{index}轮",
                conversation_id=root.id,
                conversation_turn=index,
                user_message=f"第{index}轮",
                idempotency_key=str(uuid.uuid4()),
                request_hash="a" * 64,
                source_client="web",
                status="succeeded",
                phase="finished",
                quota_state="consumed",
                quota_date=now.date(),
                prompt_version="chat-v2",
                provider=root.provider,
                model=root.model,
                config_snapshot={},
                generated_result=reply("回答"),
                image_retain_until=now + timedelta(hours=24),
                history_retain_until=now + timedelta(days=30),
            )
            db.add(turn)
            new_turns.append(turn)
        await db.commit()
        legacy = await conversations._creation_context(db, api.user_id, root.id)
        agent = await conversations._creation_context(db, api.user_id, root.id, agent_context=True)
        assert len(legacy["recent_dialogue"]) == 6
        assert len(agent["recent_dialogue"]) == 12
        assert agent["recent_dialogue"][0]["user"] == "第3轮"
        for turn in new_turns:
            turn.generated_result = reply("答" * 3500)
        await db.commit()
        agent = await conversations._creation_context(db, api.user_id, root.id, agent_context=True)
        assert len(agent["recent_dialogue"]) == 12
        assert agent["recent_dialogue"][0]["user"] == "第3轮"
        assert agent["recent_dialogue"][-1]["user"] == "第14轮"
        assert len(agent["recent_dialogue"]) <= 12
        assert all(len(turn["assistant"]) <= 2000 for turn in agent["recent_dialogue"])
        assert sum(len(turn["user"]) + len(turn["assistant"]) for turn in agent["recent_dialogue"]) <= 12000


async def test_new_reference_image_starts_an_agent_context_branch(api, sessions, monkeypatch):
    from tests.test_prompt_vision import valid_result

    provider(monkeypatch, [valid_result(), reply(), reply(), reply()])
    image_a, image_b = await upload(api), await upload(api)
    first = response_data(
        await create_conversation(api, {"asset_id": image_a["id"], "message": "图片A"}), 202
    )
    first = await _complete(api, sessions, first)
    second = response_data(await send_message(api, first, "描述图片A"), 202)
    second = await _complete(api, sessions, second)
    third = response_data(await send_message(api, second, "换成图片B", asset_id=image_b["id"]), 202)
    third = await _complete(api, sessions, third)
    async with sessions() as db:
        branch = await db.get(PromptAnalysis, uuid.UUID(third["id"]))
        branch.config_snapshot = {**branch.config_snapshot, "reference_branch_started": True}
        await db.commit()
    fourth = response_data(await send_message(api, third, "继续处理图片B"), 202)
    await _complete(api, sessions, fourth)
    async with sessions() as db:
        context = await conversations._creation_context(
            db,
            api.user_id,
            uuid.UUID(first["conversation_id"]),
            agent_context=True,
        )
        assert context["prompt"] is None
        assert context["recent_messages"] == ["换成图片B", "继续处理图片B"]
        legacy = await conversations._creation_context(db, api.user_id, uuid.UUID(first["conversation_id"]))
        assert "图片A" in legacy["recent_messages"]


@pytest.mark.parametrize("agent_version,should_reset", [("agent-v1", True), ("chat-v2", False)])
async def test_new_upload_only_resets_agent_branch_when_a_previous_image_exists(
    api, sessions, monkeypatch, agent_version, should_reset
):
    from app.services.prompt_analysis import api as api_service

    provider(monkeypatch, [reply()])
    image_a, image_b = await upload(api), await upload(api)
    first = response_data(
        await create_conversation(api, {"asset_id": image_a["id"], "message": "图片A"}), 202
    )
    first = await _complete(api, sessions, first)
    monkeypatch.setattr(api_service, "new_conversation_version", lambda _mode: agent_version)
    second = response_data(
        await send_message(api, first, "改用图片B", mode="smart", asset_id=image_b["id"]), 202
    )
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        assert bool(job.config_snapshot.get("reference_branch_started")) is should_reset
        assert (job.context_snapshot is None) is should_reset


async def test_worker_reads_only_latest_owned_uploaded_reference(api, sessions, monkeypatch, objects):
    from types import SimpleNamespace

    from app.workers import prompt_analysis as worker

    provider(monkeypatch, [reply()])
    uploaded = await upload(api)
    first = response_data(
        await create_conversation(api, {"asset_id": uploaded["id"], "message": "看图"}), 202
    )
    await _complete(api, sessions, first)
    async with sessions() as db:
        asset = await db.get(PromptAnalysisAsset, uuid.UUID(uploaded["id"]))
        expected = objects[asset.object_key]
    claim = SimpleNamespace(user_id=api.user_id, conversation_id=uuid.UUID(first["conversation_id"]))
    assert await worker._latest_reference_image(sessions, claim) == expected
    claim.user_id = uuid.uuid4()
    assert await worker._latest_reference_image(sessions, claim) is None


async def test_agent_snapshot_does_not_claim_expired_generated_image(api, sessions, monkeypatch, configured):
    from unittest.mock import AsyncMock

    from pydantic import SecretStr

    from app.services.vision import image_adapter
    from tests.test_prompt_api_integration import image_bytes

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "image"}), 202)
    first = await _complete(api, sessions, first)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        job.image_retain_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    second = response_data(await send_message(api, first, "刚才的图片是什么", mode="smart"), 202)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        assert job.context_snapshot["has_reference_image"] is False
        assert job.context_snapshot["has_generated_image"] is False


@pytest.mark.parametrize("agent_mode", [True, False])
async def test_unrelated_generated_image_starts_branch_only_for_agent(
    api, sessions, monkeypatch, configured, agent_mode,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import SecretStr

    from app.schemas.prompt_conversation import ConversationImageIntent
    from app.services import vision
    from app.services.prompt_analysis import api as api_service
    from app.services.vision import image_adapter
    from tests.test_prompt_api_integration import image_bytes
    from tests.test_prompt_vision import valid_result

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(monkeypatch, [valid_result()])
    first = response_data(await create_conversation(api, {"message": "A_旧画面", "mode": "generate"}), 202)
    first = await _complete(api, sessions, first)
    assert first["status"] == "succeeded"

    async def decision(message, **_kwargs):
        edit = message == "给B换背景"
        return vision.VisionResult(
            ConversationImageIntent(schema_version="image-intent-1", use_reference=edit, use_context=edit),
            None, {},
        )

    if agent_mode:
        monkeypatch.setattr(vision, "agent", SimpleNamespace(generate_conversation=decision), raising=False)
    else:
        monkeypatch.setattr(api_service, "new_conversation_version", lambda _mode: "chat-v2")
        monkeypatch.setattr(vision, "generate_conversation", decision)
    generate = AsyncMock(return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generate)
    second = response_data(await send_message(api, first, "B_新画面", mode="smart"), 202)
    second = await _complete(api, sessions, second)
    assert second["status"] == "succeeded"
    assert "A_旧画面" not in generate.await_args.args[0]
    third = response_data(await send_message(api, second, "给B换背景", mode="smart"), 202)
    third = await _complete(api, sessions, third)
    assert third["status"] == "succeeded"
    prompt = generate.await_args.args[0]
    assert "B_新画面" in prompt
    assert ("A_旧画面" not in prompt) is agent_mode


async def test_failed_independent_generation_does_not_start_branch(api, sessions, monkeypatch, configured):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic import SecretStr

    from app.schemas.prompt_conversation import ConversationImageIntent
    from app.services import vision
    from app.services.vision import image_adapter
    from tests.test_prompt_vision import valid_result

    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    provider(monkeypatch, [valid_result()])
    first = response_data(await create_conversation(api, {"message": "A_旧画面", "mode": "generate"}), 202)
    first = await _complete(api, sessions, first)
    decision = vision.VisionResult(
        ConversationImageIntent(schema_version="image-intent-1", use_reference=False, use_context=False),
        None, {},
    )
    monkeypatch.setattr(
        vision, "agent", SimpleNamespace(generate_conversation=AsyncMock(return_value=decision)),
        raising=False,
    )
    monkeypatch.setattr(
        image_adapter, "generate", AsyncMock(side_effect=vision.VisionError("image_provider_error")),
    )
    second = response_data(await send_message(api, first, "B_新画面", mode="smart"), 202)
    second = await _complete(api, sessions, second)
    assert second["status"] == "failed"
    async with sessions() as db:
        context = await conversations._creation_context(
            db, api.user_id, uuid.UUID(first["id"]), agent_context=True,
        )
        assert context["recent_messages"] == ["A_旧画面"]
