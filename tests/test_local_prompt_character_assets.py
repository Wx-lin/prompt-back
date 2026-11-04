"""Character assets stay in the isolated local preview database and require real auth."""

import importlib
import os

import httpx
import pytest

from app.database import get_db
from tests.test_local_prompt_preview import OTHER, OWNER, TOKEN, profile, settings
from tests.test_prompt_api_integration import sessions as sessions


@pytest.mark.asyncio
async def test_local_character_assets_require_authentication(monkeypatch):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    app = importlib.import_module("scripts.local_prompt_preview").create_app(settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            response = await client.get("/api/character-assets")
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis required",
)
async def test_local_character_assets_use_verified_identity_and_isolated_database(monkeypatch, sessions):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    calls = []

    def upstream(request):
        calls.append(request.url.path)
        identity = OTHER if request.headers["authorization"].endswith("other-user-token") else OWNER
        return httpx.Response(200, json=profile(identity=identity))

    app = importlib.import_module("scripts.local_prompt_preview").create_app(
        settings(), transport=httpx.MockTransport(upstream)
    )

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    owner = {"authorization": "Bearer " + TOKEN}
    other = {"authorization": "Bearer other-user-token"}
    payload = {
        "name": "本地测试角色",
        "values": {"name": "本地测试角色"},
        "template": "basic",
        "model": "通用",
        "preview_image_base64": None,
        "preview_signature": None,
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            created = await client.post("/api/character-assets", headers=owner, json=payload)
            assert created.status_code == 201, created.text
            identity = created.json()["data"]["id"]
            own = await client.get("/api/character-assets", headers=owner)
            assert [value["id"] for value in own.json()["data"]] == [identity]
            assert (await client.get("/api/character-assets", headers=other)).json()["data"] == []
            assert (await client.get("/api/character-assets/" + identity, headers=other)).status_code == 404
            updated = await client.put(
                "/api/character-assets/" + identity, headers=owner, json={**payload, "name": "更新角色"}
            )
            assert updated.json()["data"]["name"] == "更新角色"
            assert (
                await client.delete("/api/character-assets/" + identity, headers=owner)
            ).status_code == 204
    assert calls == ["/api/auth/me"] * 6
