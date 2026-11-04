"""Character preview is bounded for visitors and never leaks provider failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from redis.exceptions import RedisError

from app.redis import get_redis_client
from app.routers import character_preview
from app.services import character_preview as service
from app.utils.deps import get_current_user


@pytest_asyncio.fixture
async def client(monkeypatch):
    app = FastAPI()
    app.include_router(character_preview.router)
    user = SimpleNamespace(id=uuid4())
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_redis_client] = lambda: redis
    settings = SimpleNamespace(openai_api_key=SecretStr('test-key'))
    monkeypatch.setattr(service, 'get_settings', lambda: settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as api:
        yield api, redis, settings


@pytest.mark.asyncio
async def test_preview_returns_image_and_no_store(client, monkeypatch):
    api, redis, _ = client
    generate = AsyncMock(return_value='aW1hZ2U=')
    monkeypatch.setattr(service, 'generate_image', generate)
    response = await api.post('/api/character-preview', json={'prompt': 'a person'})
    assert response.status_code == 200
    assert response.json()['data'] == {'image_base64': 'aW1hZ2U=', 'mime_type': 'image/png'}
    assert response.json()['success'] is True
    assert response.json()['error'] is None
    assert response.headers['cache-control'] == 'private, no-store'
    generate.assert_awaited_once_with('a person', 'test-key', '1024x1024')
    assert redis.eval.await_count == 2  # acquire quota and release the running request lock


@pytest.mark.asyncio
async def test_preview_rate_limits_use_authenticated_identity(client, monkeypatch):
    api, redis, _ = client
    generate = AsyncMock(return_value='aW1hZ2U=')
    monkeypatch.setattr(service, 'generate_image', generate)
    response = await api.post('/api/character-preview', json={'prompt': 'person'})
    assert response.status_code == 200
    keys = redis.eval.await_args_list[0].args[2:6]
    redis.eval.reset_mock()
    response = await api.post('/api/character-preview', json={'prompt': 'person'}, headers={
        'authorization': 'Bearer forged-token', 'x-forwarded-for': '1.2.3.4',
    })
    assert response.status_code == 200
    assert redis.eval.await_args_list[0].args[2:6] == keys  # never trust forwarded headers
    assert 'shared-visitor' not in str(keys)


@pytest.mark.asyncio
async def test_preview_requires_authentication(client, monkeypatch):
    app = FastAPI()
    app.include_router(character_preview.router)
    async def deny():
        from fastapi import HTTPException
        raise HTTPException(401, '未登录')
    app.dependency_overrides[get_current_user] = deny
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    app.dependency_overrides[get_redis_client] = lambda: redis
    settings = SimpleNamespace(openai_api_key=SecretStr('test-key'))
    monkeypatch.setattr(service, 'get_settings', lambda: settings)
    monkeypatch.setattr(service, 'generate_image', AsyncMock(return_value='aW1hZ2U='))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as api:
        response = await api.post('/api/character-preview', json={'prompt': 'person'})
    assert response.status_code == 401
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', ['', '   ', 'x' * 12001])
async def test_preview_rejects_invalid_prompts_without_generation(client, monkeypatch, prompt):
    api, redis, _ = client
    generate = AsyncMock()
    monkeypatch.setattr(service, 'generate_image', generate)
    response = await api.post('/api/character-preview', json={'prompt': prompt})
    assert response.status_code == 422
    redis.eval.assert_not_awaited()
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_configuration_fails_before_rate_limit(client, monkeypatch):
    api, redis, settings = client
    settings.openai_api_key = SecretStr('')
    generate = AsyncMock()
    monkeypatch.setattr(service, 'generate_image', generate)
    response = await api.post('/api/character-preview', json={'prompt': 'person'})
    assert response.status_code == 503
    redis.eval.assert_not_awaited()
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,expected', [(0, 429), (RedisError(), 503)])
async def test_limit_or_redis_failure_never_calls_provider(client, monkeypatch, outcome, expected):
    api, redis, _ = client
    if isinstance(outcome, Exception):
        redis.eval.side_effect = outcome
    else:
        redis.eval.return_value = outcome
    generate = AsyncMock()
    monkeypatch.setattr(service, 'generate_image', generate)
    response = await api.post('/api/character-preview', json={'prompt': 'person'})
    assert response.status_code == expected
    assert response.json()['error']['code'] == ('RATE_LIMITED' if expected == 429 else 'PREVIEW_UNAVAILABLE')
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_request_is_bounded_and_provider_error_does_not_leak_key(monkeypatch):
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(400, json={'error': {'message': 'test-key private-provider-error'}})
    monkeypatch.setattr(service.httpx, 'AsyncHTTPTransport', lambda **_: httpx.MockTransport(handler))
    with pytest.raises(service.PreviewError) as error:
        await service.generate_image('person', 'test-key', '1024x1024')
    assert 'test-key' not in str(error.value)
    assert 'private-provider-error' not in str(error.value)
    import json
    payload = json.loads(requests[0].content)
    assert payload == {
        'model': 'gpt-image-2', 'prompt': 'person', 'size': '1024x1024',
        'quality': 'low', 'n': 1, 'output_format': 'png',
    }
    assert requests[0].headers['authorization'] == 'Bearer test-key'


@pytest.mark.asyncio
async def test_preview_body_is_bounded_before_json_parsing():
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as api:
        response = await api.post('/api/character-preview', content=b'x' * (128 * 1024 + 1))
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'PAYLOAD_TOO_LARGE'


@pytest.mark.asyncio
async def test_generate_image_accepts_provider_png(monkeypatch):
    import base64
    image = base64.b64encode(b'\x89PNG\r\n\x1a\nhello').decode()
    async def handler(request):
        return httpx.Response(200, json={'data': [{'b64_json': image}]})
    monkeypatch.setattr(service.httpx, 'AsyncHTTPTransport', lambda **_: httpx.MockTransport(handler))
    assert await service.generate_image('person', 'test-key', '1024x1024') == image


@pytest.mark.asyncio
async def test_provider_failure_releases_running_request_lock(client, monkeypatch):
    api, redis, _ = client
    monkeypatch.setattr(service, 'generate_image', AsyncMock(side_effect=service.PreviewError(502, '失败')))
    response = await api.post('/api/character-preview', json={'prompt': 'person'})
    assert response.status_code == 502
    assert redis.eval.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(('ratio', 'size'), [
    ('1:1', '1024x1024'), ('3:4', '864x1152'), ('4:3', '1152x864'),
    ('9:16', '864x1536'), ('16:9', '1536x864'), ('21:9', '1344x576'),
])
async def test_ratio_maps_to_bounded_provider_dimensions(client, monkeypatch, ratio, size):
    api, _, _ = client
    generate = AsyncMock(return_value='aW1hZ2U=')
    monkeypatch.setattr(service, 'generate_image', generate)
    response = await api.post('/api/character-preview', json={'prompt': 'person', 'aspect_ratio': ratio})
    assert response.status_code == 200
    generate.assert_awaited_once_with('person', 'test-key', size)


@pytest.mark.asyncio
async def test_custom_ratio_is_rejected(client):
    api, redis, _ = client
    response = await api.post('/api/character-preview', json={'prompt': 'person', 'aspect_ratio': '9999:1'})
    assert response.status_code == 422
    redis.eval.assert_not_awaited()
