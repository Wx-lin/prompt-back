import asyncio
import base64
import io
import os
import runpy
import struct
import zlib
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import inspect

from app.redis import get_redis_client
from app.routers import character_preview
from app.services import character_preview as preview_service
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import (  # noqa: F401
    configured,
    image_bytes,
    limiter,
    objects,
    response_data,
)
from tests.test_prompt_api_integration import sessions as sessions


@pytest.mark.asyncio
async def test_preview_requires_login_before_provider(monkeypatch):
    app = FastAPI()
    app.include_router(character_preview.router)
    redis = AsyncMock()
    app.dependency_overrides[get_redis_client] = lambda: redis
    generate = AsyncMock()
    monkeypatch.setattr(preview_service, "create_preview", generate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/character-preview", json={"prompt": "person"})
    assert response.status_code == 401
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
async def test_asset_round_trip_and_owner_isolation(api):
    picture = base64.b64encode(image_bytes()).decode()
    body = {
        "name": "测试角色",
        "values": {"name": "小明"},
        "template": "basic",
        "model": "general",
        "preview_image_base64": picture,
        "preview_signature": "version-1",
    }
    response_data(await api.client.get("/api/character-assets"), 401)
    created = response_data(await api.client.post("/api/character-assets", json=body, headers=api.owner), 201)
    asset_id = created["id"]
    assert created["preview_image_base64"] == picture
    listing = response_data(await api.client.get("/api/character-assets", headers=api.owner))
    assert len(listing) == 1 and listing[0]["id"] == asset_id and listing[0]["has_preview"]
    assert "preview_image_base64" not in listing[0]
    assert response_data(await api.client.get("/api/character-assets", headers=api.other)) == []
    response_data(await api.client.get("/api/character-assets/" + asset_id, headers=api.other), 404)
    response_data(
        await api.client.get("/api/character-assets/" + asset_id + "/image", headers=api.other), 404
    )
    response_data(
        await api.client.put("/api/character-assets/" + asset_id, json=body, headers=api.other), 404
    )
    response_data(await api.client.delete("/api/character-assets/" + asset_id, headers=api.other), 404)
    image = await api.client.get("/api/character-assets/" + asset_id + "/image", headers=api.owner)
    assert image.status_code == 200 and image.content == image_bytes()
    updated = response_data(
        await api.client.put(
            "/api/character-assets/" + asset_id,
            json={**body, "name": "修改后", "preview_signature": "version-2"},
            headers=api.owner,
        )
    )
    assert updated["name"] == "修改后" and updated["preview_signature"] == "version-2"
    response_data(await api.client.delete("/api/character-assets/" + asset_id, headers=api.owner), 204)
    response_data(await api.client.get("/api/character-assets/" + asset_id, headers=api.owner), 404)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
@pytest.mark.parametrize("bad", ["not-base64!", base64.b64encode(b"not png").decode()])
async def test_asset_rejects_invalid_image(api, bad):
    body = {
        "name": "角色",
        "values": {},
        "template": "basic",
        "model": "general",
        "preview_image_base64": bad,
        "preview_signature": None,
    }
    response_data(await api.client.post("/api/character-assets", json=body, headers=api.owner), 422)


def test_preview_image_size_cap_and_values_validation():
    from pydantic import ValidationError

    from app.schemas.character_asset import MAX_IMAGE_BYTES, CharacterAssetInput

    assert MAX_IMAGE_BYTES == 15 * 1024 * 1024
    body = {"name": "角色", "values": {}, "template": "basic", "model": "general"}
    with pytest.raises(ValidationError):
        CharacterAssetInput(
            **body, preview_image_base64=base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode()
        )
    with pytest.raises(ValidationError):
        CharacterAssetInput(**{**body, "values": {"field": "x" * 5001}})


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
async def test_generated_png_over_five_megabytes_can_be_saved_and_restored(api):
    width, height = 1400, 1400
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG", compress_level=0)
    content = encoded.getvalue()
    assert 5 * 1024 * 1024 < len(content) < 15 * 1024 * 1024
    image64 = base64.b64encode(content).decode()
    body = {
        "name": "大图角色",
        "values": {},
        "template": "basic",
        "model": "通用",
        "preview_image_base64": image64,
        "preview_signature": "s",
    }
    response = await api.client.post("/api/character-assets", json=body, headers=api.owner)
    assert response.status_code == 201
    asset_id = response.json()["data"]["id"]
    restored = await api.client.get("/api/character-assets/" + asset_id + "/image", headers=api.owner)
    assert restored.status_code == 200 and restored.content == content


@pytest.mark.asyncio
async def test_asset_body_boundary_allows_base64_json_up_to_21_mib():
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        valid_boundary = await client.post("/api/character-assets", content=b"x" * (8 * 1024 * 1024))
        over_boundary = await client.post("/api/character-assets", content=b"x" * (21 * 1024 * 1024 + 1))
    assert valid_boundary.status_code != 413
    assert over_boundary.status_code == 413


def test_rejects_png_with_corrupt_compressed_data_even_when_crc_is_valid():
    from pydantic import ValidationError

    from app.schemas.character_asset import CharacterAssetInput

    original = image_bytes()
    offset = 8
    chunks = [original[:8]]
    while offset < len(original):
        length = struct.unpack(">I", original[offset : offset + 4])[0]
        chunk_type = original[offset + 4 : offset + 8]
        chunk = original[offset + 8 : offset + 8 + length]
        if chunk_type == b"IDAT":
            chunk = b"\x00" * length
        chunks.append(
            struct.pack(">I", length) + chunk_type + chunk + struct.pack(">I", zlib.crc32(chunk_type + chunk))
        )
        offset += 12 + length
    body = {
        "name": "角色",
        "values": {},
        "template": "basic",
        "model": "general",
        "preview_image_base64": base64.b64encode(b"".join(chunks)).decode(),
    }
    with pytest.raises(ValidationError):
        CharacterAssetInput(**body)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
async def test_concurrent_asset_creations_cannot_exceed_user_limit(api, monkeypatch):
    from app.routers import character_assets

    monkeypatch.setattr(character_assets, "MAX_ASSETS_PER_USER", 1, raising=False)
    body = {"name": "角色", "values": {}, "template": "basic", "model": "general"}
    results = await asyncio.gather(
        *(api.client.post("/api/character-assets", json=body, headers=api.owner) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [201, 429]
    assert len(response_data(await api.client.get("/api/character-assets", headers=api.owner))) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
async def test_image_quota_applies_to_create_and_update_and_frees_on_delete(api, monkeypatch):
    from app.routers import character_assets

    picture = base64.b64encode(image_bytes()).decode()
    monkeypatch.setattr(character_assets, "MAX_STORAGE_BYTES_PER_USER", len(image_bytes()) + 1, raising=False)
    body = {"name": "角色", "values": {}, "template": "basic", "model": "general"}
    first = response_data(
        await api.client.post(
            "/api/character-assets", json={**body, "preview_image_base64": picture}, headers=api.owner
        ),
        201,
    )
    second = response_data(await api.client.post("/api/character-assets", json=body, headers=api.owner), 201)
    response_data(
        await api.client.post(
            "/api/character-assets", json={**body, "preview_image_base64": picture}, headers=api.owner
        ),
        413,
    )
    response_data(
        await api.client.put(
            "/api/character-assets/" + second["id"],
            json={**body, "preview_image_base64": picture},
            headers=api.owner,
        ),
        413,
    )
    response_data(
        await api.client.put(
            "/api/character-assets/" + first["id"],
            json={**body, "preview_image_base64": picture},
            headers=api.owner,
        )
    )
    response_data(await api.client.delete("/api/character-assets/" + first["id"], headers=api.owner), 204)
    response_data(
        await api.client.put(
            "/api/character-assets/" + second["id"],
            json={**body, "preview_image_base64": picture},
            headers=api.owner,
        )
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
async def test_concurrent_image_updates_cannot_exceed_storage_quota(api, monkeypatch):
    from app.routers import character_assets

    picture = base64.b64encode(image_bytes()).decode()
    monkeypatch.setattr(character_assets, "MAX_STORAGE_BYTES_PER_USER", len(image_bytes()) + 1)
    body = {"name": "角色", "values": {}, "template": "basic", "model": "general"}
    first = response_data(await api.client.post("/api/character-assets", json=body, headers=api.owner), 201)
    second = response_data(await api.client.post("/api/character-assets", json=body, headers=api.owner), 201)
    responses = await asyncio.gather(
        *(
            api.client.put(
                "/api/character-assets/" + item["id"],
                json={**body, "preview_image_base64": picture},
                headers=api.owner,
            )
            for item in (first, second)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 413]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="isolated PostgreSQL and Redis URLs required",
)
async def test_character_asset_migration_round_trip(sessions):
    migration = runpy.run_path(
        str(Path(__file__).parents[1] / "alembic/versions/d93c70f6b321_character_assets.py")
    )

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            migration[direction]()

    async with sessions() as db:
        connection = await db.connection()
        await connection.run_sync(lambda conn: migrate(conn, "downgrade"))
        assert "character_assets" not in await connection.run_sync(
            lambda conn: inspect(conn).get_table_names()
        )
        await connection.run_sync(lambda conn: migrate(conn, "upgrade"))
        columns = await connection.run_sync(
            lambda conn: {c["name"] for c in inspect(conn).get_columns("character_assets")}
        )
        assert {"user_id", "values", "template", "model", "preview_image", "preview_signature"} <= columns
        await db.commit()
