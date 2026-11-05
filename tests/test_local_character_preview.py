"""The standalone local preview stays authenticated and loopback-only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.redis import get_redis_client
from app.utils.deps import get_current_user


def local_settings(**overrides):
    return Settings(
        _env_file=None, app_env="local-character-preview", redis_url="redis://127.0.0.1:6379/12", **overrides
    )


def test_local_app_requires_opt_in_and_loopback_redis(monkeypatch):
    from scripts.local_character_preview import create_app

    monkeypatch.delenv("AIGC_LOCAL_CHARACTER_PREVIEW", raising=False)
    with pytest.raises(RuntimeError):
        create_app(local_settings())
    monkeypatch.setenv("AIGC_LOCAL_CHARACTER_PREVIEW", "1")
    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None, app_env="production", redis_url="redis://127.0.0.1:6379/12"))
    with pytest.raises(RuntimeError):
        create_app(
            Settings(
                _env_file=None, app_env="local-character-preview", redis_url="redis://example.com:6379/12"
            )
        )
    with pytest.raises(RuntimeError):
        create_app(
            Settings(_env_file=None, app_env="local-character-preview", redis_url="redis://127.0.0.1:6379/0")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host,headers",
    [
        ("192.0.2.1", {}),
        ("127.0.0.1", {"host": "attacker.example"}),
        ("127.0.0.1", {"origin": "https://attacker.example"}),
    ],
)
async def test_local_app_rejects_untrusted_sources(monkeypatch, host, headers):
    from scripts.local_character_preview import create_app

    monkeypatch.setenv("AIGC_LOCAL_CHARACTER_PREVIEW", "1")
    app = create_app(local_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, client=(host, 1)), base_url="http://127.0.0.1:8051"
    ) as client:
        response = await client.post("/api/character-preview", json={"prompt": "test"}, headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_local_app_allows_repeated_generation_without_redis_limits(monkeypatch):
    from app.services import character_preview
    from scripts.local_character_preview import create_app

    monkeypatch.setenv("AIGC_LOCAL_CHARACTER_PREVIEW", "1")
    generate = AsyncMock(return_value="aW1hZ2U=")
    monkeypatch.setattr(character_preview, "generate_image", generate)
    app = create_app(local_settings(openai_api_key="test-key"))
    redis = SimpleNamespace(eval=AsyncMock(return_value=0))
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="test-user")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8051"
    ) as client:
        responses = [
            await client.post("/api/character-preview", json={"prompt": "test", "aspect_ratio": "16:9"})
            for _ in range(2)
        ]
        assert (await client.get("/api/auth/me")).status_code == 404
    assert [response.status_code for response in responses] == [200, 200]
    assert all(
        response.json()["data"] == {"image_base64": "aW1hZ2U=", "mime_type": "image/png"}
        for response in responses
    )
    assert generate.await_count == 2
    generate.assert_awaited_with("test", "test-key", "1536x864")
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_app_still_requires_authentication(monkeypatch):
    from scripts.local_character_preview import create_app

    monkeypatch.setenv("AIGC_LOCAL_CHARACTER_PREVIEW", "1")
    app = create_app(local_settings(openai_api_key="test-key"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8051"
    ) as client:
        response = await client.post("/api/character-preview", json={"prompt": "test"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_local_app_supports_character_interpretation(monkeypatch):
    from app.services import character_interpret
    from scripts.local_character_preview import create_app

    monkeypatch.setenv('AIGC_LOCAL_CHARACTER_PREVIEW', '1')
    monkeypatch.setattr(character_interpret, 'require_configuration', lambda: None)
    monkeypatch.setattr(character_interpret, 'generate_fields', AsyncMock(return_value={
        'values': {'hairColor': '黑色'}, 'unmapped': ''
    }))
    app = create_app(local_settings(openai_api_key='test-key'))
    app.dependency_overrides[get_redis_client] = lambda: SimpleNamespace(eval=AsyncMock(return_value=1))
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='test-user')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:8051'
    ) as client:
        response = await client.post('/api/character-interpret', json={'description': '黑发'})
    assert response.status_code == 200
    assert response.json()['data']['values'] == {'hairColor': '黑色'}
