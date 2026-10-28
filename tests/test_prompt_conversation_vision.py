"""Conversation provider transport is bounded, private and never auto-retried."""

import json
import logging

import httpx2
import pytest

from app.services.vision import VisionError, generate_conversation
from tests.test_prompt_vision import completion_response, install_transport, valid_result
from tests.test_prompt_vision import configured as configured


def context():
    result = valid_result()
    return {
        "title": result["title"],
        "prompt": result["prompt"],
        "breakdown": result["breakdown"],
        "source_type": "image",
        "recent_messages": ["参考图片", "保留构图"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"context": {}},
        {"message": "猫" * 501},
        {"message": 4},
        {"image": "not bytes"},
        {"timeout": float("nan")},
        {"timeout": 0},
        {"context": {**context(), "extra": True}},
        {"context": {**context(), "recent_messages": ["猫"] * 7}},
    ],
)
async def test_invalid_conversation_input_never_calls_provider(configured, monkeypatch, changes):
    calls = []
    install_transport(monkeypatch, lambda request: calls.append(request))
    arguments = {"message": "改成白天", "context": context(), "image": None, "timeout": 2, **changes}
    with pytest.raises(VisionError) as error:
        await generate_conversation(**arguments)
    assert error.value.code == "model_invalid_input"
    assert calls == []


@pytest.mark.asyncio
async def test_conversation_text_and_image_share_strict_bilingual_output(configured, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(valid_result()))

    install_transport(monkeypatch, upstream)
    result = await generate_conversation("保留构图改成白天", context=context(), image=None, timeout=2)
    assert result.result.model_dump() == valid_result()
    assert isinstance(calls[0]["messages"][1]["content"], str)
    assert "不可信" in calls[0]["messages"][0]["content"]
    assert json.loads(calls[0]["messages"][1]["content"])["context"] == {
        **context(), "recent_dialogue": [],
    }
    configured.prompt_vision_provider = "dashscope"
    await generate_conversation("", context=None, image=b"private-image", timeout=2)
    assert calls[1]["enable_thinking"] is False
    assert calls[1]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[1]
    assert calls[1]["messages"][1]["content"][1]["type"] == "image_url"
    assert "保留构图改成白天" not in caplog.text and "test-only-key" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,uncertain", [(429, False), (500, True)])
async def test_conversation_provider_failure_is_safe_and_single_call(
    configured, monkeypatch, status, uncertain
):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx2.Response(status, json={"error": {"message": "private upstream payload"}})

    install_transport(monkeypatch, upstream)
    with pytest.raises(VisionError) as error:
        await generate_conversation("猫", context=None, image=None, timeout=2)
    assert error.value.uncertain is uncertain and len(calls) == 1
    assert "private" not in str(error.value)
