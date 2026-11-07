import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr

from app.schemas.prompt_conversation import (
    ConversationImageIntent,
    ConversationImageQuestion,
    ConversationReplyResult,
)
from app.services.vision import agent
from app.services.vision.adapter import VisionError


class RecordingModel(FakeMessagesListChatModel):
    calls: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        return super()._generate(messages, stop, run_manager, **kwargs)


def call(name, args, call_id="call-1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


@pytest.fixture
def use_model(monkeypatch):
    monkeypatch.setattr(agent.adapter, "require_vision_configuration", lambda: None)
    monkeypatch.setattr(agent, "_image_available", lambda: True)

    def use(responses):
        model = RecordingModel(responses=responses)
        monkeypatch.setattr(agent, "_make_model", lambda settings, timeout: model)
        return model

    return use


@pytest.mark.asyncio
async def test_capability_question_gets_tool_result_and_never_requests_generation(use_model):
    model = use_model(
        [call("get_creation_capabilities", {}), AIMessage(content="可以，告诉我你想生成什么画面。")]
    )
    result = await agent.generate_conversation(
        "你能生图吗？", context=None, image=None, timeout=10, mode="smart"
    )
    assert isinstance(result.result, ConversationReplyResult)
    assert "可以" in result.result.reply
    assert len(model.calls) == 2
    assert any(isinstance(message, ToolMessage) and "生图" in message.content for message in model.calls[1])


@pytest.mark.asyncio
async def test_image_tool_returns_validated_intent_without_provider_call(use_model):
    model = use_model([call("request_image_generation", {"use_reference": False, "use_context": False})])
    result = await agent.generate_conversation("画一只猫", context=None, image=None, timeout=10, mode="smart")
    assert result.result == ConversationImageIntent(
        schema_version="image-intent-1", use_reference=False, use_context=False
    )
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_previous_image_question_returns_existing_worker_decision(use_model):
    use_model([call("view_previous_image", {})])
    context = {
        "title": "之前的画",
        "prompt": None,
        "breakdown": None,
        "source_type": None,
        "recent_messages": [],
        "recent_dialogue": [],
        "has_generated_image": True,
    }
    result = await agent.generate_conversation(
        "刚才的图里是什么？", context=context, image=None, timeout=10, mode="smart"
    )
    assert isinstance(result.result, ConversationImageQuestion)


@pytest.mark.asyncio
async def test_missing_reference_is_explained_without_dispatching_image(use_model):
    use_model([call("request_image_generation", {"use_reference": True, "use_context": False})])
    result = await agent.generate_conversation(
        "按参考图画", context=None, image=None, timeout=10, mode="smart"
    )
    assert isinstance(result.result, ConversationReplyResult)
    assert "图" in result.result.reply


@pytest.mark.asyncio
async def test_current_image_and_recent_dialogue_are_passed_to_the_model(use_model):
    model = use_model([AIMessage(content="画面是一个小猫。")])
    image = b"\x89PNG\r\n\x1a\nexample"
    context = {
        "title": "猫",
        "prompt": None,
        "breakdown": None,
        "source_type": None,
        "recent_messages": ["你好"],
        "recent_dialogue": [{"user": "你好", "assistant": "你好呀"}],
        "has_generated_image": False,
    }
    await agent.generate_conversation("这是什么？", context=context, image=image, timeout=10, mode="smart")
    messages = model.calls[0]
    assert any(isinstance(item, HumanMessage) and item.content == "你好" for item in messages)
    assert any(isinstance(item, AIMessage) and item.content == "你好呀" for item in messages)
    final = messages[-1]
    assert isinstance(final, HumanMessage)
    assert any(
        block.get("image_url", {}).get("url", "").endswith(base64.b64encode(image).decode())
        for block in final.content
        if isinstance(block, dict)
    )


@pytest.mark.asyncio
async def test_multiple_tools_in_one_turn_are_rejected(use_model):
    model = use_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "request_image_generation",
                        "args": {"use_reference": False, "use_context": False},
                        "id": "one",
                        "type": "tool_call",
                    },
                    {
                        "name": "request_image_generation",
                        "args": {"use_reference": False, "use_context": False},
                        "id": "two",
                        "type": "tool_call",
                    },
                ],
            )
        ]
    )
    with pytest.raises(VisionError):
        await agent.generate_conversation("画一只猫", context=None, image=None, timeout=10, mode="smart")
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_unknown_or_malformed_tool_is_rejected_before_execution(use_model):
    use_model([call("run_arbitrary_shell", {"command": "x"})])
    with pytest.raises(VisionError):
        await agent.generate_conversation("画一只猫", context=None, image=None, timeout=10, mode="smart")
    use_model(
        [
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "name": "request_image_generation",
                        "args": "{",
                        "id": "bad",
                        "error": "invalid json",
                        "type": "invalid_tool_call",
                    }
                ],
            )
        ]
    )
    with pytest.raises(VisionError):
        await agent.generate_conversation("画一只猫", context=None, image=None, timeout=10, mode="smart")


@pytest.mark.asyncio
async def test_tool_loop_sums_usage_and_preserves_model_and_provider_id(use_model):
    first = call("get_creation_capabilities", {})
    first.usage_metadata = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    first.response_metadata = {
        "id": "first",
        "model_name": "qwen-demo",
        "token_usage": {"prompt_tokens_details": {"cached_tokens": 3}},
    }
    second = AIMessage(
        content="可以生图。",
        usage_metadata={"input_tokens": 90, "output_tokens": 15, "total_tokens": 105},
        response_metadata={
            "id": "second",
            "model_name": "qwen-demo",
            "token_usage": {"completion_tokens_details": {"reasoning_tokens": 2}},
        },
    )
    use_model([first, second])
    result = await agent.generate_conversation(
        "你能生图吗？", context=None, image=None, timeout=10, mode="smart"
    )
    assert result.usage == {
        "prompt_tokens": 190,
        "completion_tokens": 35,
        "total_tokens": 225,
        "cached_tokens": 3,
        "reasoning_tokens": 2,
    }
    assert result.model == "qwen-demo"
    assert result.provider_request_id == "second"


@pytest.mark.asyncio
async def test_explicit_prompt_is_returned_in_existing_structured_format(use_model):
    use_model(
        [
            call(
                "deliver_prompt",
                {
                    "title": "猫咪",
                    "prompt": {"zh": "一只猫", "en": "a cat"},
                    "breakdown": {
                        key: {"zh": "", "en": ""} for key in ("subject", "composition", "lighting", "style")
                    },
                    "uncertain_details": [],
                },
            )
        ]
    )
    result = await agent.generate_conversation(
        "写一段猫咪提示词", context=None, image=None, timeout=10, mode="smart"
    )
    assert result.result.schema_version == "1"
    assert result.result.prompt.zh == "一只猫"


@pytest.mark.asyncio
async def test_real_langchain_chat_openai_payload_and_tool_round_trip(monkeypatch):
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            response = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "cap-1",
                        "type": "function",
                        "function": {"name": "get_creation_capabilities", "arguments": "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            response = {"role": "assistant", "content": "可以生图，请告诉我你想画什么。"}
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"test-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen-test",
                "choices": [{"index": 0, "message": response, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    model = ChatOpenAI(
        model="qwen-test",
        api_key=SecretStr("fixture-only"),
        base_url="https://example.test/v1",
        http_async_client=client,
        max_retries=0,
        extra_body={"enable_thinking": False},
        use_responses_api=False,
    )
    monkeypatch.setattr(agent.adapter, "require_vision_configuration", lambda: None)
    monkeypatch.setattr(agent, "_make_model", lambda settings, timeout: model)
    monkeypatch.setattr(agent, "_image_available", lambda: True)
    picture = b"\x89PNG\r\n\x1a\nexample"
    result = await agent.generate_conversation(
        "你能生图吗？", context=None, image=picture, timeout=10, mode="smart"
    )
    assert isinstance(result.result, ConversationReplyResult)
    assert len(requests) == 2
    assert requests[0]["enable_thinking"] is False
    assert "生图" in requests[0]["messages"][0]["content"]
    assert "can" not in requests[0]["messages"][0]["content"]
    assert any(t["function"]["name"] == "request_image_generation" for t in requests[0]["tools"])
    assert requests[0]["messages"][-1]["content"][1]["image_url"]["url"].endswith(
        base64.b64encode(picture).decode()
    )
    assert any(m["role"] == "tool" and m["tool_call_id"] == "cap-1" for m in requests[1]["messages"])
    assert result.usage["total_tokens"] == 24
    assert result.model == "qwen-test"
    assert result.provider_request_id == "test-2"


@pytest.mark.asyncio
async def test_tools_are_bounded_and_cannot_spin_forever(use_model):
    model = use_model([call("get_creation_capabilities", {})])
    with pytest.raises(VisionError):
        await agent.generate_conversation("你能做什么", context=None, image=None, timeout=10, mode="smart")
    assert len(model.calls) <= 3


@pytest.mark.asyncio
async def test_tracing_is_disabled_even_if_environment_enables_it(monkeypatch, use_model):
    from langsmith import run_helpers

    observed = []
    original = RecordingModel._generate

    def inspect_tracing(self, *args, **kwargs):
        observed.append(run_helpers.get_tracing_context().get("enabled"))
        return original(self, *args, **kwargs)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(RecordingModel, "_generate", inspect_tracing)
    use_model([AIMessage(content="可以。")])
    await agent.generate_conversation("能生图吗？", context=None, image=None, timeout=10, mode="smart")
    assert observed == [False]


@pytest.mark.asyncio
async def test_openai_compatible_provider_keeps_max_tokens_instead_of_dashscope_options(monkeypatch):
    bodies = []

    async def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "compatible-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    monkeypatch.setattr(agent, "DefaultAsyncHttpxClient", lambda **kwargs: client)
    monkeypatch.setattr(agent.adapter, "require_vision_configuration", lambda: None)
    monkeypatch.setattr(agent, "_image_available", lambda: False)
    settings = SimpleNamespace(
        prompt_vision_provider="openai-compatible",
        prompt_vision_model="compatible-model",
        prompt_vision_api_key=SecretStr("not-a-real-key"),
        prompt_vision_base_url="https://example.test/v1",
        prompt_vision_max_tokens=1024,
    )
    monkeypatch.setattr(agent.adapter, "get_settings", lambda: settings)
    await agent.generate_conversation("你好", context=None, image=None, timeout=10, mode="smart")
    assert bodies[0]["max_tokens"] == 1024
    assert "max_completion_tokens" not in bodies[0]
    assert "enable_thinking" not in bodies[0]


@pytest.mark.asyncio
async def test_agent_retains_recent_history_with_bounded_character_budget(use_model):
    model = use_model([AIMessage(content="继续。")])
    history = [{"user": f"用户{i} " + "甲" * 490, "assistant": f"答{i} " + "乙" * 490} for i in range(12)]
    context = {
        "title": "角色",
        "prompt": {"zh": "甲" * 6000, "en": "b" * 12000},
        "breakdown": None,
        "source_type": "text",
        "recent_messages": [turn["user"] for turn in history],
        "recent_dialogue": history,
        "has_generated_image": False,
        "has_reference_image": True,
    }
    result = await agent.generate_conversation(
        "继续修改", context=context, image=None, timeout=10, mode="smart"
    )
    assert isinstance(result.result, ConversationReplyResult)
    sent = [item for item in model.calls[0] if isinstance(item, (HumanMessage, AIMessage))]
    assert sum(len(item.content) for item in sent if isinstance(item.content, str)) <= 16000
    assert any(isinstance(item, HumanMessage) and item.content.startswith("用户11 ") for item in sent)
    assert all(len(item.content) <= 2000 for item in sent[:-1] if isinstance(item, AIMessage))


@pytest.mark.asyncio
async def test_image_only_request_has_nonempty_text_for_provider(use_model):
    model = use_model([AIMessage(content="收到。")])
    await agent.generate_conversation(
        "", context=None, image=b"\x89PNG\r\n\x1a\nexample", timeout=10, mode="smart"
    )
    content = model.calls[0][-1].content
    assert any(block.get("type") == "text" and block.get("text") for block in content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question", ["你能生图吗？", "能生图不", "能不能生成图片", "你会画图吗", "Can you generate images?"]
)
async def test_capability_question_cannot_be_misclassified_as_paid_image(use_model, question):
    use_model([call("request_image_generation", {"use_reference": False, "use_context": False})])
    result = await agent.generate_conversation(question, context=None, image=None, timeout=10, mode="smart")
    assert isinstance(result.result, ConversationReplyResult)
    assert "可以" in result.result.reply


@pytest.mark.asyncio
async def test_polite_actual_image_request_is_not_blocked_as_capability_question(use_model):
    use_model([call("request_image_generation", {"use_reference": False, "use_context": False})])
    result = await agent.generate_conversation(
        "可以帮我画一只猫吗？", context=None, image=None, timeout=10, mode="smart"
    )
    assert isinstance(result.result, ConversationImageIntent)


@pytest.mark.asyncio
async def test_deleted_session_or_lost_lease_blocks_second_model_call(use_model):
    model = use_model([call("get_creation_capabilities", {}), AIMessage(content="可以。")])
    checks = iter([True, False])

    async def fence():
        return next(checks)

    with pytest.raises(VisionError, match="dispatch_fenced"):
        await agent.generate_conversation(
            "能做什么", context=None, image=None, timeout=10, mode="smart", before_model_call=fence
        )
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_worker_can_share_three_call_budget_across_two_agent_invocations(use_model):
    count = 0

    async def shared_fence():
        nonlocal count
        count += 1
        return count <= 3

    first = use_model([call("get_creation_capabilities", {}), AIMessage(content="可以。")])
    await agent.generate_conversation(
        "能做什么", context=None, image=None, timeout=10, mode="smart", before_model_call=shared_fence
    )
    assert len(first.calls) == 2
    second = use_model([call("get_creation_capabilities", {}), AIMessage(content="这是图片。")])
    with pytest.raises(VisionError, match="dispatch_fenced"):
        await agent.generate_conversation(
            "图片是什么", context=None, image=None, timeout=10, mode="smart", before_model_call=shared_fence
        )
    assert len(second.calls) == 1


def test_image_capability_requires_storage_as_well_as_provider(monkeypatch):
    from app.services.prompt_analysis.errors import AnalysisError

    monkeypatch.setattr(agent.scf_image_adapter, "image_provider_available", lambda settings: True)
    monkeypatch.setattr(
        agent.storage,
        "require_storage_configuration",
        lambda: (_ for _ in ()).throw(AnalysisError(503, "STORAGE_UNAVAILABLE", "unavailable")),
    )
    assert not agent._image_available()


@pytest.mark.asyncio
async def test_current_reference_flag_overrides_expired_legacy_image_flag(use_model):
    use_model([call("request_image_generation", {"use_reference": True, "use_context": False})])
    context = {
        "title": "之前的画",
        "prompt": None,
        "breakdown": None,
        "source_type": None,
        "recent_messages": [],
        "recent_dialogue": [],
        "has_generated_image": True,
        "has_reference_image": False,
    }
    result = await agent.generate_conversation(
        "用之前的图画一个太阳", context=context, image=None, timeout=10
    )
    assert isinstance(result.result, ConversationReplyResult)


@pytest.mark.asyncio
async def test_legacy_generated_image_flag_is_used_when_new_flag_is_absent(use_model):
    use_model([call("request_image_generation", {"use_reference": True, "use_context": False})])
    context = {
        "title": "之前的画",
        "prompt": None,
        "breakdown": None,
        "source_type": None,
        "recent_messages": [],
        "recent_dialogue": [],
        "has_generated_image": True,
    }
    result = await agent.generate_conversation(
        "用之前的图画一个太阳", context=context, image=None, timeout=10
    )
    assert isinstance(result.result, ConversationImageIntent)


@pytest.mark.asyncio
async def test_long_user_message_and_history_reach_agent_model_without_truncation(use_model):
    long_text = "猫" * 1201
    model = use_model([AIMessage(content="好的")])
    context = {
        "title": "猫", "recent_messages": [long_text],
        "recent_dialogue": [{"user": long_text, "assistant": "好的"}],
    }
    await agent.generate_conversation(long_text, context=context, image=None, timeout=10, mode="smart")
    messages = model.calls[0]
    assert any(isinstance(item, HumanMessage) and item.content == long_text for item in messages)
    assert json.loads(messages[-1].content)["当前消息"] == long_text


def test_agent_context_keeps_newest_long_user_description_whole():
    from app.services.prompt_analysis.conversations import _bounded_agent_dialogue

    long_text = "猫" * 1201
    recent = [SimpleNamespace(user_message=long_text, idea_text=None, generated_result={"reply": "好"})]
    assert _bounded_agent_dialogue(recent)[0]["user"] == long_text


def test_agent_history_budget_never_silently_slices_a_user_description():
    from app.services.prompt_analysis.conversations import _bounded_agent_dialogue

    newest = SimpleNamespace(user_message="新" * 8000, idea_text=None, generated_result={"reply": "好"})
    older = SimpleNamespace(user_message="旧" * 5000, idea_text=None, generated_result={"reply": "好"})
    dialogue = _bounded_agent_dialogue([newest, older])
    assert [turn["user"] for turn in dialogue] == [newest.user_message]
