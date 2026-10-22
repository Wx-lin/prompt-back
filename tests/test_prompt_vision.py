import json
import logging
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from app.schemas.prompt_analysis import (
    BilingualPrompt,
    CreateAnalysisRequest,
    EditAnalysisRequest,
    ModelResult,
)
from app.services.vision import VisionError, adapter, analyze_image, require_vision_configuration


def valid_result():
    return {
        "schema_version": "1",
        "title": "红色静物",
        "prompt": {"zh": "白色背景上的红色静物", "en": "A red still life on white"},
        "breakdown": {
            "subject": {"zh": "红色静物", "en": "Red still life"},
            "composition": {"zh": "", "en": ""},
            "lighting": {"zh": "柔光", "en": "Soft light"},
            "style": {"zh": "摄影", "en": "Photography"},
        },
        "uncertain_details": [],
    }


def test_strict_bilingual_schema_accepts_empty_unobservable_breakdown():
    result = ModelResult.model_validate(valid_result())
    assert result.breakdown.composition.zh == ""
    assert result.model_dump() == valid_result()
    with pytest.raises(ValidationError):
        result.title = "changed"


def test_published_schema_requires_both_observed_or_both_empty_breakdown_languages():
    schema = ModelResult.model_json_schema()
    assert schema["$defs"]["BilingualBreakdown"]["oneOf"] == [
        {"properties": {"zh": {"const": ""}, "en": {"const": ""}}},
        {"properties": {"zh": {"pattern": r"\S"}, "en": {"pattern": r"\S"}}},
    ]


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), 1),
        (("schema_version",), "2"),
        (("title",), " "),
        (("title",), "a" * 81),
        (("prompt", "zh"), "\t\n"),
        (("prompt", "en"), 123),
        (("prompt", "zh"), "a" * 6001),
        (("prompt", "en"), "a" * 12001),
        (("breakdown", "subject", "zh"), ""),
        (("breakdown", "composition", "zh"), " "),
        (("breakdown", "subject", "en"), "a" * 1201),
        (("breakdown", "subject", "zh"), "a" * 601),
        (("uncertain_details",), ["a"] * 9),
        (("uncertain_details",), [" "]),
        (("uncertain_details",), ["a" * 241]),
        (("unexpected",), "private"),
        (("prompt", "unexpected"), "private"),
        (("breakdown", "unexpected"), {}),
        (("breakdown", "subject", "unexpected"), "private"),
    ],
)
def test_strict_result_rejects_wrong_types_whitespace_limits_and_extra_fields(path, value):
    data = valid_result()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ModelResult.model_validate(data)


def test_creation_and_edit_contracts_validate_uuid_revision_and_bilingual_prompt():
    asset_id = uuid4()
    assert CreateAnalysisRequest.model_validate({"asset_id": str(asset_id)}).asset_id == asset_id
    assert EditAnalysisRequest(revision=0, prompt=BilingualPrompt(zh="你好", en="Hello")).revision == 0
    for data in ({"asset_id": "bad"}, {"asset_id": str(asset_id), "url": "https://invalid.test"}):
        with pytest.raises(ValidationError):
            CreateAnalysisRequest.model_validate(data)
    for revision in (-1, True, "1"):
        with pytest.raises(ValidationError):
            EditAnalysisRequest(revision=revision, prompt={"zh": "你好", "en": "Hello"})


@pytest.fixture
def configured(monkeypatch):
    settings = SimpleNamespace(
        prompt_vision_api_key=SecretStr("test-only-key"),
        prompt_vision_model="test-vision",
        prompt_vision_provider="openai-compatible",
        prompt_vision_base_url="https://vision.example.test/v1",
        prompt_vision_max_tokens=4096,
        prompt_analysis_prompt_version="v1",
    )
    monkeypatch.setattr(adapter, "get_settings", lambda: settings)
    return settings


def install_transport(monkeypatch, handler):
    original_client = adapter.DefaultAsyncHttpxClient
    monkeypatch.setattr(
        adapter,
        "DefaultAsyncHttpxClient",
        lambda **kwargs: original_client(transport=httpx2.MockTransport(handler), **kwargs),
    )


def completion_response(content, *, finish_reason="stop", refusal=None):
    return httpx2.Response(
        200,
        headers={"x-request-id": "req-test"},
        json={
            "id": "completion-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-vision",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content, "refusal": refusal},
                }
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 50, "total_tokens": 90},
        },
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_vision_api_key", SecretStr("")),
        ("prompt_vision_model", " "),
        ("prompt_vision_provider", "mock"),
        ("prompt_vision_base_url", "http://vision.example.test/v1"),
        ("prompt_vision_base_url", "https://key@vision.example.test/v1"),
        ("prompt_vision_base_url", "https://[bad"),
        ("prompt_vision_max_tokens", 0),
        ("prompt_analysis_prompt_version", "v2"),
    ],
)
def test_unconfigured_or_invalid_provider_fails_before_dispatch(configured, field, value):
    setattr(configured, field, value)
    with pytest.raises(VisionError) as caught:
        require_vision_configuration()
    assert caught.value.code == "model_not_configured"
    assert caught.value.uncertain is False


@pytest.mark.asyncio
async def test_one_call_returns_validated_bilingual_result_without_exposing_image(
    configured,
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.DEBUG)
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        assert request.url == "https://vision.example.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only-key"
        return completion_response(json.dumps(valid_result(), ensure_ascii=False))

    install_transport(monkeypatch, upstream)
    result = await analyze_image(b"jpeg-test-content", timeout=2)
    assert result.result.model_dump() == valid_result()
    assert result.provider_request_id == "req-test"
    assert result.usage == {"prompt_tokens": 40, "completion_tokens": 50, "total_tokens": 90}
    assert len(calls) == 1
    request = calls[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == 4096
    assert "max_completion_tokens" not in request
    assert "enable_thinking" not in request
    assert request["model"] == "test-vision"
    assert "不可信" in request["messages"][0]["content"]
    assert "schema_version" in request["messages"][0]["content"]
    assert "data:image/jpeg;base64," in request["messages"][1]["content"][0]["image_url"]["url"]
    assert "jpeg-test-content" not in caplog.text
    assert "data:image" not in caplog.text
    assert "test-only-key" not in caplog.text
    with pytest.raises(FrozenInstanceError):
        result.provider_request_id = "changed"


@pytest.mark.asyncio
async def test_dashscope_disables_thinking_and_uses_completion_token_limit(configured, monkeypatch):
    configured.prompt_vision_provider = "dashscope"
    configured.prompt_vision_model = "qwen3.8-max"
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(valid_result()))

    install_transport(monkeypatch, upstream)
    await analyze_image(b"jpeg", timeout=2)
    assert len(calls) == 1
    assert calls[0]["model"] == "qwen3.8-max"
    assert calls[0]["enable_thinking"] is False
    assert calls[0]["max_completion_tokens"] == 4096
    assert "max_tokens" not in calls[0]
    assert calls[0].get("stream", False) is False


@pytest.mark.asyncio
async def test_generic_provider_is_not_inferred_from_dashscope_hostname(configured, monkeypatch):
    configured.prompt_vision_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return completion_response(json.dumps(valid_result()))

    install_transport(monkeypatch, upstream)
    await analyze_image(b"jpeg", timeout=2)
    assert calls[0]["max_tokens"] == 4096
    assert "max_completion_tokens" not in calls[0]
    assert "enable_thinking" not in calls[0]


def parsed_response(*, model="qwen3.8-max-0902", usage=None, include_model=True):
    return adapter._parse_response(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(refusal=None, content=json.dumps(valid_result())),
                )
            ],
            id="completion-test",
            usage=usage,
            **({"model": model} if include_model else {}),
        )
    )


@pytest.mark.parametrize(
    "model,expected",
    [
        (" qwen3.8-max-0902 ", "qwen3.8-max-0902"),
        ("a" * 250, "a" * 200),
        (None, None),
        (123, None),
        (True, None),
        ({"private": "payload"}, None),
        (" \t ", None),
        ("qwen3.8-max\nprivate", None),
    ],
)
def test_actual_response_model_is_optional_bounded_and_safe(model, expected):
    result = parsed_response(model=model)
    assert result.model == expected
    with pytest.raises(FrozenInstanceError):
        result.model = "changed"


def test_missing_response_model_is_optional():
    assert parsed_response(include_model=False).model is None


def test_token_details_preserve_provider_totals_without_double_counting_reasoning():
    usage = SimpleNamespace(
        prompt_tokens=40,
        completion_tokens=50,
        total_tokens=90,
        prompt_tokens_details=SimpleNamespace(image_tokens=30, text_tokens=10, cached_tokens=3),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=8, text_tokens=42),
    )
    assert parsed_response(usage=usage).usage == {
        "prompt_tokens": 40,
        "completion_tokens": 50,
        "total_tokens": 90,
        "image_tokens": 30,
        "text_input_tokens": 10,
        "cached_tokens": 3,
        "reasoning_tokens": 8,
        "text_output_tokens": 42,
    }


@pytest.mark.parametrize("invalid", [None, True, -1, 1.5, "42"])
def test_invalid_token_details_are_not_saved_as_accounting(invalid):
    usage = SimpleNamespace(
        prompt_tokens=invalid,
        completion_tokens=invalid,
        total_tokens=invalid,
        prompt_tokens_details=SimpleNamespace(
            image_tokens=invalid, text_tokens=invalid, cached_tokens=invalid
        ),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=invalid, text_tokens=invalid),
    )
    assert parsed_response(usage=usage).usage == {}


def test_zero_and_missing_token_details_are_valid():
    usage = SimpleNamespace(prompt_tokens=0, prompt_tokens_details=SimpleNamespace(image_tokens=0))
    assert parsed_response(usage=usage).usage == {"prompt_tokens": 0, "image_tokens": 0}


def test_vision_result_keeps_existing_constructor_compatible():
    result = adapter.VisionResult(ModelResult.model_validate(valid_result()), None, {})
    assert result.model is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,uncertain", [(400, False), (401, False), (429, False), (500, True), (503, True)]
)
async def test_upstream_errors_never_trigger_sdk_retries_or_expose_provider_payload(
    configured, monkeypatch, status, uncertain
):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx2.Response(status, json={"error": {"message": "private provider payload"}})

    install_transport(monkeypatch, upstream)
    with pytest.raises(VisionError) as caught:
        await analyze_image(b"jpeg", timeout=2)
    assert len(calls) == 1
    assert caught.value.uncertain is uncertain
    assert "private provider payload" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx2.ReadTimeout, httpx2.ConnectError])
async def test_network_failures_are_uncertain_without_automatic_resend(configured, monkeypatch, error):
    calls = []

    def upstream(request):
        calls.append(request)
        raise error("private network detail", request=request)

    install_transport(monkeypatch, upstream)
    with pytest.raises(VisionError) as caught:
        await analyze_image(b"jpeg", timeout=2)
    assert caught.value.uncertain is True
    assert caught.value.code in {"model_timeout", "model_connection_error"}
    assert len(calls) == 1
    assert "private network detail" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,finish_reason,refusal",
    [
        ("not json private text", "stop", None),
        ("{}", "stop", None),
        (json.dumps(valid_result()), "length", None),
        (None, "stop", "refused"),
    ],
)
async def test_invalid_truncated_or_refused_output_is_known_failure_without_repair_calls(
    configured,
    monkeypatch,
    content,
    finish_reason,
    refusal,
):
    calls = []

    def upstream(request):
        calls.append(request)
        return completion_response(content, finish_reason=finish_reason, refusal=refusal)

    install_transport(monkeypatch, upstream)
    with pytest.raises(VisionError) as caught:
        await analyze_image(b"jpeg", timeout=2)
    assert caught.value.uncertain is False
    assert caught.value.code in {"model_invalid_output", "model_refused"}
    assert "private text" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_config_never_constructs_a_network_client(configured, monkeypatch):
    configured.prompt_vision_api_key = SecretStr("")

    def forbidden_client(**kwargs):
        pytest.fail("client must not be constructed without configuration")

    monkeypatch.setattr(adapter, "DefaultAsyncHttpxClient", forbidden_client)
    with pytest.raises(VisionError, match="model_not_configured"):
        await analyze_image(b"jpeg", timeout=2)


@pytest.mark.asyncio
async def test_non_json_provider_response_is_safe_known_failure(configured, monkeypatch):
    install_transport(
        monkeypatch,
        lambda _: httpx2.Response(
            200,
            text="private malformed response",
            headers={"Content-Type": "application/json"},
        ),
    )
    with pytest.raises(VisionError, match="model_invalid_output") as caught:
        await analyze_image(b"jpeg", timeout=2)
    assert caught.value.uncertain is False
