"""Image generation uses distinct provider endpoints and the durable chat contract."""

import base64
import io
import json
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from app.schemas.prompt_conversation import ConversationInput


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(output, format='PNG')
    return output.getvalue()


def test_conversation_accepts_explicit_image_mode():
    body = ConversationInput(message='a red flower', mode='image')
    assert body.mode == 'image'


def test_generated_reference_is_image_mode_only_and_cannot_conflict_with_upload():
    from pydantic import ValidationError
    selected = uuid4()
    body = ConversationInput(message='change it', mode='image', generated_image_id=selected)
    assert body.generated_image_id == selected
    with pytest.raises(ValidationError):
        ConversationInput(message='change it', mode='auto', generated_image_id=selected)
    with pytest.raises(ValidationError):
        ConversationInput(message='change it', mode='image', generated_image_id=selected, asset_id=uuid4())


def test_image_prompt_includes_bounded_prior_context_and_current_instruction():
    from app.services.vision.image_adapter import build_image_prompt
    prompt = build_image_prompt("根据上面生成", {
        "prompt": {"zh": "海边小猫"},
        "recent_dialogue": [{"user": "加夕阳", "assistant": "夕阳下的小猫"}],
    })
    assert "海边小猫" in prompt and "加夕阳" in prompt and "根据上面生成" in prompt
    assert prompt.rfind("根据上面生成") > prompt.rfind("海边小猫")
    assert len(build_image_prompt("画图", {"prompt": {"zh": "x" * 20000}})) < 7000


@pytest.mark.asyncio
@pytest.mark.parametrize(('reference', 'endpoint'), [(None, 'generations'), (image_bytes(), 'edits')])
async def test_provider_selects_generation_or_image_edit(monkeypatch, reference, endpoint):
    from app.services.vision import image_adapter

    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(image_bytes()).decode()}]})
    monkeypatch.setattr(image_adapter.httpx, 'AsyncHTTPTransport', lambda **_: httpx.MockTransport(handler))
    result = await image_adapter.generate('make it red', api_key='test-key', reference=reference, timeout=5)
    assert result == image_bytes()
    assert calls[0].url.path == '/v1/images/' + endpoint
    assert calls[0].headers['authorization'] == 'Bearer test-key'
    if reference:
        assert b'make it red' in calls[0].content
        assert b'name="image"' in calls[0].content
    else:
        assert json.loads(calls[0].content)['prompt'] == 'make it red'
