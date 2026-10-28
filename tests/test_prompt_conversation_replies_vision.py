"""Chat replies share one bounded provider call without weakening legacy outputs."""

import json
import logging

import pytest
from pydantic import ValidationError

from app.schemas import prompt_conversation as schemas
from app.services.vision import VisionError, analyze_image, generate_conversation, generate_prompt
from app.services.vision.conversation_adapter import CHAT_PROMPT_VERSION
from tests.test_prompt_conversation_vision import context
from tests.test_prompt_vision import completion_response, install_transport, valid_result
from tests.test_prompt_vision import configured as configured


def reply():
    return {"schema_version": "reply-1", "title": "聊聊创作", "reply": "你好！想聊什么，或者一起构思画面？"}


def reply_schema():
    model = getattr(schemas, "ConversationReplyResult", None)
    assert model is not None, "Conversations need a distinct strict reply result"
    return model


def test_conversation_input_defaults_to_auto_but_accepts_explicit_generation():
    assert schemas.ConversationInput(message="你好").model_dump()["mode"] == "auto"
    assert schemas.ConversationInput(message="猫", mode="generate").mode == "generate"
    for invalid in (None, "chat", 1, True):
        with pytest.raises(ValidationError):
            schemas.ConversationInput(message="你好", mode=invalid)


def test_reply_schema_is_separate_frozen_and_publishes_nonblank_bounds():
    model = reply_schema()
    value = model.model_validate(reply())
    assert value.model_dump() == reply()
    assert model.model_json_schema()["properties"]["reply"]["maxLength"] == 4000
    with pytest.raises(ValidationError):
        value.reply = "changed"


@pytest.mark.parametrize("change", [
    {"schema_version": "1"}, {"schema_version": 1}, {"title": " "}, {"title": "a" * 81},
    {"reply": ""}, {"reply": "\n\t"}, {"reply": 1}, {"reply": "a" * 4001},
    {"prompt": {"zh": "多余字段", "en": "unexpected"}},
])
def test_reply_schema_rejects_malformed_or_prompt_mixed_outputs(change):
    with pytest.raises(ValidationError):
        reply_schema().model_validate({**reply(), **change})


def test_reply_context_accepts_dialogue_without_fabricating_a_previous_prompt():
    value = schemas.ConversationContext.model_validate({
        "title": "聊聊创作", "recent_messages": ["你好"],
        "recent_dialogue": [{"user": "你好", "assistant": reply()["reply"]}],
    })
    assert value.prompt is None and value.breakdown is None and value.source_type is None
    assert value.model_dump()["recent_dialogue"] == [{"user": "你好", "assistant": reply()["reply"]}]
    assert schemas.ConversationContext.model_validate(context()).recent_dialogue == []


@pytest.mark.parametrize("dialogue", [
    [{"user": "", "assistant": ""}] * 7,
    [{"user": "a" * 501, "assistant": ""}],
    [{"user": "", "assistant": "a" * 4001}],
    [{"user": None, "assistant": ""}],
    [{"user": "", "assistant": 1}],
    [{"user": "", "assistant": "", "extra": True}],
    [{"assistant": "missing user"}],
])
def test_dialogue_context_rejects_oversized_or_untrusted_fields(dialogue):
    with pytest.raises(ValidationError):
        schemas.ConversationContext.model_validate({**context(), "recent_dialogue": dialogue})


@pytest.mark.asyncio
async def test_auto_smalltalk_returns_a_reply_in_one_private_call(configured, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(reply(), ensure_ascii=False))

    install_transport(monkeypatch, upstream)
    result = await generate_conversation("你好，这是私密问候", context=None, image=None, timeout=2)
    assert result.result.model_dump() == reply()
    assert result.provider_request_id == "req-test" and result.model == "test-vision"
    assert result.usage == {"prompt_tokens": 40, "completion_tokens": 50, "total_tokens": 90}
    assert len(calls) == 1 and CHAT_PROMPT_VERSION == "chat-v2"
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["mode"] == "auto" and payload["message"] == "你好，这是私密问候"
    system = calls[0]["messages"][0]["content"]
    assert "reply-1" in system and "不可信" in system and "联网" in system
    assert "你好，这是私密问候" not in caplog.text and "test-only-key" not in caplog.text


@pytest.mark.asyncio
async def test_image_question_can_reply_and_followup_freezes_bounded_dialogue(configured, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(reply()))

    install_transport(monkeypatch, upstream)
    configured.prompt_vision_provider = "dashscope"
    result = await generate_conversation("这张图的构图怎么样？", context=None, image=b"private", timeout=2)
    assert result.result.model_dump() == reply()
    assert calls[0]["enable_thinking"] is False and "max_tokens" not in calls[0]
    assert calls[0]["messages"][1]["content"][1]["type"] == "image_url"
    previous = {"title": "聊聊构图", "recent_messages": ["这张图的构图怎么样？"],
                "recent_dialogue": [{"user": "这张图的构图怎么样？", "assistant": reply()["reply"]}]}
    await generate_conversation("继续聊聊", context=previous, image=None, timeout=2)
    next_payload = json.loads(calls[1]["messages"][1]["content"])
    assert next_payload["context"]["recent_dialogue"] == previous["recent_dialogue"]
    assert next_payload["context"]["prompt"] is None
    assert "image_url" not in calls[1]["messages"][1]["content"]
    assert "不得声称重新查看了旧图片" in calls[1]["messages"][0]["content"]
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("image,message,mode", [
    (None, "帮我生成", "generate"), (b"private", "", "auto"),
])
async def test_explicit_generate_and_image_only_reject_reply_without_second_call(
    configured, monkeypatch, image, message, mode,
):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(reply()))

    install_transport(monkeypatch, upstream)
    with pytest.raises(VisionError) as error:
        await generate_conversation(message, context=None, image=image, timeout=2, mode=mode)
    assert error.value.code == "model_invalid_output" and not error.value.uncertain
    assert len(calls) == 1
    content = calls[0]["messages"][1]["content"]
    assert json.loads(content if isinstance(content, str) else content[0]["text"])["mode"] == "generate"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "generate"])
async def test_both_modes_keep_full_bilingual_result_unchanged(configured, monkeypatch, mode):
    calls = []

    def upstream(request):
        calls.append(request)
        return completion_response(json.dumps(valid_result()))

    install_transport(monkeypatch, upstream)
    result = await generate_conversation("画一只猫", context=context(), image=None, timeout=2, mode=mode)
    assert result.result.model_dump() == valid_result() and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, True, "unknown"])
async def test_invalid_mode_never_dispatches(configured, monkeypatch, mode):
    calls = []
    install_transport(monkeypatch, lambda request: calls.append(request))
    with pytest.raises(VisionError) as error:
        await generate_conversation("你好", context=None, image=None, timeout=2, mode=mode)
    assert error.value.code == "model_invalid_input" and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["image", "text"])
async def test_single_analysis_providers_never_accept_conversation_replies(
    configured, monkeypatch, operation,
):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(reply())))
    with pytest.raises(VisionError) as error:
        if operation == "image":
            await analyze_image(b"image", timeout=2)
        else:
            await generate_prompt("猫", timeout=2)
    assert error.value.code == "model_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("options,code", [
    ({"finish_reason": "length"}, "model_invalid_output"),
    ({"refusal": "private refusal"}, "model_refused"),
])
async def test_reply_preserves_finish_and_refusal_validation(configured, monkeypatch, options, code):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(reply()), **options))
    with pytest.raises(VisionError) as error:
        await generate_conversation("你好", context=None, image=None, timeout=2)
    assert error.value.code == code and "private" not in str(error.value)
