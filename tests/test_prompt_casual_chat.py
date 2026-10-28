"""Durable mixed conversations, using isolated real SQL/Redis and provider transport."""

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.models import PromptAnalysis, PromptAnalysisAttempt
from app.services import vision
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import response_data
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation, send_message
from tests.test_prompt_vision import completion_response, install_transport, valid_result

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis URLs required",
    ),
]


def reply(text="你好！今天想聊些什么？"):
    return {"schema_version": "reply-1", "title": "日常对话", "reply": text}


def provider(monkeypatch, results):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        value = results[min(len(calls) - 1, len(results) - 1)]
        return completion_response(json.dumps(value, ensure_ascii=False))

    install_transport(monkeypatch, upstream)
    return calls


async def complete(api, sessions, turn):
    assert await run_once(sessions)
    value = response_data(await api.client.get("/api/prompt-analyses/" + turn["id"], headers=api.owner))
    assert value["status"] == "succeeded", value
    return value


async def conversation(api, root):
    return response_data(await api.client.get(
        "/api/prompt-conversations/" + root["id"], headers=api.owner,
    ))


async def test_real_reply_is_saved_restored_private_and_not_a_prompt(api, sessions, monkeypatch):
    answer = reply("你好！可以聊聊今天的心情，也可以一起想点创作灵感。")
    calls = provider(monkeypatch, [answer])
    key = str(uuid.uuid4())
    first = response_data(await create_conversation(api, {"message": "你好呀"}, key=key), 202)
    ready = await complete(api, sessions, first)
    assert ready["response_type"] == "assistant"
    assert ready["assistant_reply"] == answer["reply"]
    assert ready["prompt"] is None and ready["result"] is None
    assert ready["prompt_number"] is None and ready["saved_to_history"] is True
    restored = await conversation(api, first)
    assert restored["turns"][0]["assistant_reply"] == answer["reply"]
    replay = response_data(await create_conversation(api, {"message": "你好呀"}, key=key))
    assert replay["id"] == first["id"] and len(calls) == 1
    response_data(await api.client.get(
        "/api/prompt-conversations/" + first["id"], headers=api.other,
    ), 404)
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        assert stored.generated_result == answer
        assert len(list(await db.scalars(select(PromptAnalysisAttempt)))) == 1


async def test_followup_keeps_assistant_answer_for_references_without_a_prompt(api, sessions, monkeypatch):
    calls = provider(monkeypatch, [reply("可以聊旅行，也可以聊音乐。"), reply("那我们聊音乐吧。")])
    first = response_data(await create_conversation(api, {"message": "推荐两个聊天话题"}), 202)
    first = await complete(api, sessions, first)
    second = response_data(await send_message(api, first, "第二个吧"), 202)
    second = await complete(api, sessions, second)
    context = json.loads(calls[1]["messages"][1]["content"])["context"]
    assert context["prompt"] is None
    assert context["recent_dialogue"][-1] == {
        "user": "推荐两个聊天话题", "assistant": "可以聊旅行，也可以聊音乐。",
    }
    assert second["parent_analysis_id"] == first["id"]
    assert second["version"] == 2 and second["prompt_number"] is None


async def test_prompt_consultation_and_revision_preserve_latest_real_edited_prompt(
    api, sessions, monkeypatch,
):
    calls = provider(monkeypatch, [valid_result(), reply("浅景深可以突出主体。"), valid_result()])
    root = response_data(await create_conversation(api), 202)
    first = await complete(api, sessions, root)
    assert first["prompt_number"] == 1
    chat = response_data(await send_message(api, first, "浅景深是什么意思？"), 202)
    chat = await complete(api, sessions, chat)
    edited = {**first["prompt"], "zh": "已保存的人工编辑：保留橘猫和花束"}
    response_data(await api.client.patch(
        "/api/prompt-analyses/" + first["id"], headers=api.owner,
        json={"revision": first["revision"], "prompt": edited},
    ))
    third = response_data(await send_message(api, chat, "那背景清楚些，光线暖一点"), 202)
    third = await complete(api, sessions, third)
    context = json.loads(calls[2]["messages"][1]["content"])["context"]
    assert context["prompt"] == edited
    assert context["recent_dialogue"][-1]["assistant"] == "浅景深可以突出主体。"
    assert third["parent_analysis_id"] == chat["id"]
    assert third["version"] == 3 and third["prompt_number"] == 2
    turns = (await conversation(api, root))["turns"]
    assert [turn["prompt_number"] for turn in turns] == [1, None, 2]
    response_data(await send_message(api, first, "旧页面的消息"), 409)
    assert len(calls) == 3


async def test_chat_then_explicit_generation_freezes_mode_in_idempotency(api, sessions, monkeypatch):
    calls = provider(monkeypatch, [reply(), valid_result()])
    root = response_data(await create_conversation(api, {"message": "你好"}), 202)
    first = await complete(api, sessions, root)
    key = str(uuid.uuid4())
    body = {
        "message": "雨夜送花的小猫", "mode": "generate",
        "parent_analysis_id": first["id"], "parent_revision": first["revision"],
    }
    path = "/api/prompt-conversations/" + first["id"] + "/messages"
    headers = {**api.owner, "Idempotency-Key": key}
    second = response_data(await api.client.post(path, headers=headers, json=body), 202)
    second = await complete(api, sessions, second)
    assert second["response_type"] == "prompt" and second["prompt_number"] == 1
    assert second["assistant_reply"] is None and len(calls) == 2
    error = response_data(await api.client.post(path, headers=headers, json={**body, "mode": "auto"}), 409)
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(second["id"]))
        assert stored.config_snapshot["conversation_mode"] == "generate"


async def test_reply_cannot_be_edited_into_a_prompt_and_delete_scrubs_both_messages(
    api, sessions, monkeypatch,
):
    provider(monkeypatch, [reply()])
    root = response_data(await create_conversation(api, {"message": "你好"}), 202)
    first = await complete(api, sessions, root)
    error = response_data(await api.client.patch(
        "/api/prompt-analyses/" + first["id"], headers=api.owner,
        json={"revision": 0, "prompt": valid_result()["prompt"]},
    ), 409)
    assert error["code"] == "NOT_EDITABLE"
    response_data(await api.client.delete(
        "/api/prompt-conversations/" + root["id"], headers=api.owner,
    ), 204)
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(root["id"]))
        assert stored.deleted_at is not None and stored.generated_result is None
        assert stored.user_message is None and stored.context_snapshot is None
        assert stored.quota_state == "consumed"


async def test_legacy_single_text_job_never_accepts_an_assistant_result(api, sessions, monkeypatch):
    async def invalid_provider(*args, **kwargs):
        return SimpleNamespace(result=reply(), usage={}, provider_request_id="test-only")

    monkeypatch.setattr(vision, "generate_prompt", invalid_provider)
    first = response_data(await api.client.post(
        "/api/prompt-analyses", headers={**api.owner, "Idempotency-Key": str(uuid.uuid4())},
        json={"input_type": "text", "idea_text": "小猫壁纸"},
    ), 202)
    assert await run_once(sessions)
    value = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert value["status"] == "failed" and value["result"] is None and value["prompt"] is None


async def test_prompt_context_survives_more_than_six_casual_messages(api, sessions, configured, monkeypatch):
    configured.prompt_analysis_daily_limit = 20
    calls = provider(
        monkeypatch, [valid_result(), *[reply(f"普通回复{i}") for i in range(7)], valid_result()],
    )
    root = response_data(await create_conversation(api), 202)
    first = await complete(api, sessions, root)
    latest = first
    for index in range(7):
        latest = response_data(await send_message(api, latest, f"普通聊天{index}"), 202)
        latest = await complete(api, sessions, latest)
    final = response_data(await send_message(api, latest, "把之前的小猫背景改暖一些"), 202)
    final = await complete(api, sessions, final)
    context = json.loads(calls[-1]["messages"][1]["content"])["context"]
    assert context["prompt"] == first["prompt"]
    assert len(context["recent_dialogue"]) == 6
    assert context["recent_dialogue"][-1]["assistant"] == "普通回复6"
    assert final["prompt_number"] == 2


async def test_full_conversation_numbers_prompts_without_per_turn_count_queries(api, sessions, monkeypatch):
    provider(monkeypatch, [valid_result(), reply(), valid_result()])
    root = response_data(await create_conversation(api), 202)
    latest = await complete(api, sessions, root)
    latest = response_data(await send_message(api, latest, "你好"), 202)
    latest = await complete(api, sessions, latest)
    latest = response_data(await send_message(api, latest, "背景换成海边"), 202)
    await complete(api, sessions, latest)
    statements = []
    engine = sessions.kw["bind"].sync_engine

    def observed(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", observed)
    try:
        turns = (await conversation(api, root))["turns"]
    finally:
        event.remove(engine, "before_cursor_execute", observed)
    assert [turn["prompt_number"] for turn in turns] == [1, None, 2]
    assert not any("count(" in sql and "prompt_analyses" in sql for sql in statements)


async def test_explicit_retry_keeps_the_original_generation_mode(api, sessions, monkeypatch):
    calls = []

    async def controlled(message, *, context, image, timeout, mode="auto"):
        calls.append(mode)
        if len(calls) == 1:
            raise vision.VisionError("model_upstream_error")
        return SimpleNamespace(result=valid_result(), usage={}, provider_request_id="test-only")

    monkeypatch.setattr(vision, "generate_conversation", controlled)
    first = response_data(await create_conversation(api, {
        "message": "猫的壁纸", "mode": "generate",
    }), 202)
    assert await run_once(sessions)
    failed = response_data(await api.client.get(
        "/api/prompt-analyses/" + first["id"], headers=api.owner,
    ))
    assert failed["status"] == "failed"
    assert failed["response_type"] is None and failed["assistant_reply"] is None
    retried = response_data(await api.client.post(
        "/api/prompt-conversations/" + first["id"] + "/retry",
        headers={**api.owner, "Idempotency-Key": str(uuid.uuid4())},
        json={"analysis_id": first["id"]},
    ), 202)
    completed = await complete(api, sessions, retried)
    assert completed["prompt_number"] == 1 and calls == ["generate", "generate"]


async def test_forced_generation_rejects_reply_at_persistence_boundary(api, sessions, monkeypatch):
    calls = []

    async def invalid(message, *, context, image, timeout, mode="auto"):
        calls.append(mode)
        return SimpleNamespace(result=reply(), usage={}, provider_request_id="test-only")

    monkeypatch.setattr(vision, "generate_conversation", invalid)
    first = response_data(await create_conversation(api, {
        "message": "猫的壁纸", "mode": "generate",
    }), 202)
    assert await run_once(sessions)
    failed = response_data(await api.client.get(
        "/api/prompt-analyses/" + first["id"], headers=api.owner,
    ))
    assert failed["status"] == "failed" and failed["error_code"] == "model_invalid_output"
    assert failed["result"] is None and failed["assistant_reply"] is None
    assert calls == ["generate"]
