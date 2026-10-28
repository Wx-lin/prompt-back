"""Local-only preview bridge: real private API/SQL, bounded authenticated source reads."""

import importlib
import os
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import get_db
from app.models import Prompt, PromptAnalysisAsset, PromptImage, User
from app.services.prompt_analysis.errors import AnalysisError
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions

OWNER = uuid.UUID("00000000-0000-4000-8000-000000000011")
OTHER = uuid.UUID("00000000-0000-4000-8000-000000000012")
PROMPT = uuid.UUID("00000000-0000-4000-8000-000000000021")
IMAGE = uuid.UUID("00000000-0000-4000-8000-000000000022")
STAMP = "2026-10-27T00:00:00+00:00"
TOKEN = str(uuid.UUID("00000000-0000-4000-8000-000000000099"))


def entry():
    return importlib.import_module("scripts.local_prompt_preview")


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "database_url": "postgresql+asyncpg://preview@127.0.0.1:5432/aigc_prompt_preview_test",
            "redis_url": "redis://127.0.0.1:6379/12",
            "app_env": "development",
            **overrides,
        },
    )


def profile(identity=OWNER, nickname="真实昵称"):
    return {
        "id": str(identity),
        "email": "real@example.com",
        "nickname": nickname,
        "avatar_url": "https://example.com/avatar.png",
        "created_at": STAMP,
    }


def public_prompt():
    return {
        "id": str(PROMPT),
        "title": "真实公开作品",
        "prompt_text": "真实的完整提示词原文",
        "category": "product",
        "description": "原作者说明",
        "models": ["Midjourney V7"],
        "author_name": "真实作者",
        "author_avatar_url": None,
        "source_url": "https://example.com/source",
        "like_count": 7,
        "collect_count": 2,
        "view_count": 100,
        "created_at": STAMP,
        "images": [
            {
                "id": str(IMAGE),
                "image_url": "https://preview-test.cos.ap-guangzhou.myqcloud.com/prompts/original.jpg",
                "thumbnail_url": None,
                "sort_order": 0,
            }
        ],
    }


def test_preview_entry_exists_without_importing_the_production_app():
    assert callable(entry().create_app)


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "production"},
        {"database_url": "postgresql+asyncpg://x@production.example/aigc_prompt_preview_test"},
        {"database_url": "postgresql+asyncpg://x@127.0.0.1/aigc_prompt_dev"},
        {"database_url": "sqlite+aiosqlite:///preview.db"},
        {"database_url": "postgresql+asyncpg://x@127.0.0.1/aigc_prompt_preview_test?host=production.example"},
        {"redis_url": "redis://production.example/12"},
        {"redis_url": "redis://127.0.0.1/0"},
        {"redis_url": "redis://127.0.0.1/12?db=0"},
    ],
)
def test_refuses_unsafe_runtime_configuration(monkeypatch, overrides):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    with pytest.raises(RuntimeError, match="local preview"):
        entry().create_app(settings(**overrides))


def test_requires_an_explicit_local_marker(monkeypatch):
    monkeypatch.delenv("AIGC_LOCAL_PROMPT_PREVIEW", raising=False)
    with pytest.raises(RuntimeError, match="local preview"):
        entry().create_app(settings())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,host",
    [
        ({"host": "untrusted.example"}, "127.0.0.1"),
        ({"origin": "https://untrusted.example"}, "127.0.0.1"),
        ({}, "192.0.2.1"),
    ],
)
async def test_preview_refuses_nonlocal_clients_hosts_and_origins(monkeypatch, headers, host):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    app = entry().create_app(settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, client=(host, 1234)), base_url="http://127.0.0.1:8040"
        ) as client:
            response = await client.get("/health", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_health_is_local_and_public_or_production_auth_routes_are_absent(monkeypatch):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    app = entry().create_app(settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok", "local_prompt_preview": True}
            for path in ["/api/auth/me", "/api/auth/google", "/api/admin/prompts", "/api/prompts"]:
                assert (await client.get(path)).status_code == 404
            response = await client.get("/api/prompt-conversations")
            assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,status",
    [("expired", 401), ("redirect", 503), ("invalid", 503), ("large", 503), ("html", 503), ("timeout", 503)],
)
async def test_source_auth_fails_closed_and_never_echoes_upstream_content(monkeypatch, kind, status):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    calls = []

    def upstream(request):
        calls.append(request)
        assert str(request.url) == "http://127.0.0.1:8000/api/auth/me"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if kind == "timeout":
            raise httpx.ReadTimeout("private token", request=request)
        if kind == "expired":
            return httpx.Response(401, json={"detail": "private identity"})
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://untrusted.example/private"})
        if kind == "large":
            return httpx.Response(200, json={"private": "x" * 40000})
        if kind == "html":
            return httpx.Response(200, text="private upstream html")
        return httpx.Response(200, json={"id": "private invalid user"})

    app = entry().create_app(settings(), transport=httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            response = await client.get(
                "/api/prompt-conversations", headers={"authorization": "Bearer " + TOKEN}
            )
    assert response.status_code == status
    assert "private" not in str(response.json()).replace("private, no-store", "")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_boundary_rejects_large_creative_body_before_authentication(monkeypatch):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    calls = []
    app = entry().create_app(settings(), transport=httpx.MockTransport(lambda request: calls.append(request)))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            response = await client.post("/api/prompt-conversations", content="x" * (128 * 1024 + 1))
    assert response.status_code == 413 and not calls


@pytest.mark.parametrize(
    "header", [None, "", "Basic x", "Bearer ", "Bearer invalid token", "Bearer " + "x" * 8193]
)
def test_only_bounded_bearer_credentials_are_forwarded(header):
    source_module = importlib.import_module("scripts.local_prompt_preview_source")
    with pytest.raises(AnalysisError) as error:
        source_module.bearer_token(header)
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"created_at": "2026-10-27T00:00:00"},
        {"nickname": "x" * 101},
        {"avatar_url": "x" * 501},
    ],
)
async def test_unusable_remote_identity_is_not_projected(changes):
    source_module = importlib.import_module("scripts.local_prompt_preview_source")
    source = source_module.PreviewSource(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={**profile(), **changes}),
        )
    )
    async with source.client:
        with pytest.raises(AnalysisError) as error:
            await source.current_user("Bearer " + TOKEN)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_source_stream_limit_is_enforced_without_content_length():
    source_module = importlib.import_module("scripts.local_prompt_preview_source")

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"private":"'
            yield b"x" * 40000
            yield b'"}'

    source = source_module.PreviewSource(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Chunks(), headers={"content-type": "application/json"}),
        )
    )
    async with source.client:
        with pytest.raises(AnalysisError) as error:
            await source.current_user("Bearer " + TOKEN)
    assert error.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected", [("withdrawn", 404), ("wrong-id", 503), ("no-images", 503), ("wrong-host", 422)]
)
async def test_public_source_never_seeds_missing_or_untrusted_gallery_content(monkeypatch, kind, expected):
    source_module = importlib.import_module("scripts.local_prompt_preview_source")
    monkeypatch.setattr(get_settings(), "cos_bucket", "preview-test")
    data = public_prompt()
    if kind == "wrong-id":
        data = {**data, "id": str(OTHER)}
    if kind == "no-images":
        data = {**data, "images": []}
    if kind == "wrong-host":
        data = {
            **data,
            "images": [{**data["images"][0], "image_url": "https://untrusted.example/private.jpg"}],
        }
    calls = []

    def upstream(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(404 if kind == "withdrawn" else 200, json=data)

    source = source_module.PreviewSource(transport=httpx.MockTransport(upstream))
    async with source.client:
        with pytest.raises(AnalysisError) as error:
            await source.public_prompt(PROMPT)
    assert error.value.status_code == expected and len(calls) == 1


@pytest.mark.asyncio
async def test_public_source_refuses_arbitrary_paths_before_network():
    source_module = importlib.import_module("scripts.local_prompt_preview_source")
    calls = []
    source = source_module.PreviewSource(transport=httpx.MockTransport(lambda request: calls.append(request)))
    async with source.client:
        with pytest.raises(AnalysisError) as error:
            await source.public_prompt("../../private")
    assert error.value.status_code == 422 and not calls


@pytest.mark.asyncio
async def test_ambiguous_request_headers_fail_before_authentication(monkeypatch):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    app = entry().create_app(settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:8040",
        ) as client:
            response = await client.get(
                "/health", headers=[("host", "127.0.0.1:8040"), ("host", "127.0.0.1:8040")]
            )
    assert response.status_code == 403


INTEGRATION = pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="Explicit isolated PostgreSQL and Redis required",
)


@pytest.mark.asyncio
@INTEGRATION
async def test_remote_auth_is_rechecked_and_mirrors_only_verified_identity(monkeypatch, sessions):
    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=profile(nickname="首次昵称" if len(calls) == 1 else "更新昵称"))

    app = entry().create_app(settings(), transport=httpx.MockTransport(upstream))

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            for _ in range(2):
                response = await client.get(
                    "/api/prompt-conversations", headers={"authorization": "Bearer " + TOKEN}
                )
                assert response.status_code == 200, response.text
    assert len(calls) == 2
    async with sessions() as db:
        user = await db.get(User, OWNER)
        assert user.nickname == "更新昵称" and user.is_active
        assert user.email is None and user.google_id is None
        assert user.avatar_url == profile()["avatar_url"]


@pytest.mark.asyncio
@INTEGRATION
async def test_gallery_import_projects_real_public_fields_then_uses_existing_private_cos_flow(
    monkeypatch, sessions, configured, objects, limiter
):
    from app.redis import get_redis_client

    monkeypatch.setenv("AIGC_LOCAL_PROMPT_PREVIEW", "1")
    monkeypatch.setattr(get_settings(), "cos_bucket", "preview-test")
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json=profile())
        assert request.url.path == f"/api/prompts/{PROMPT}"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=public_prompt())

    from app.services.prompt_analysis import storage
    from tests.test_prompt_api_integration import image_bytes

    async def source_image(key):
        assert key == "prompts/original.jpg"
        return image_bytes(), "image/png"

    monkeypatch.setattr(storage, "read_public_prompt", source_image)
    app = entry().create_app(settings(), transport=httpx.MockTransport(upstream))

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_redis_client] = lambda: limiter
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8040"
        ) as client:
            response = await client.post(
                "/api/prompt-assets/from-prompt",
                headers={"authorization": "Bearer " + TOKEN},
                json={"prompt_id": str(PROMPT)},
            )
            assert response.status_code == 201, response.text
    asset_id = uuid.UUID(response.json()["data"]["id"])
    assert len(calls) == 2 and objects
    async with sessions() as db:
        prompt = await db.get(Prompt, PROMPT)
        image = await db.get(PromptImage, IMAGE)
        asset = await db.get(PromptAnalysisAsset, asset_id)
        assert (
            prompt.title == public_prompt()["title"] and prompt.prompt_text == public_prompt()["prompt_text"]
        )
        assert prompt.author_name == "真实作者" and prompt.source_url == public_prompt()["source_url"]
        assert image.image_url == public_prompt()["images"][0]["image_url"]
        assert asset.user_id == OWNER and asset.state == "ready"
        assert (await db.scalars(select(PromptImage))).all() == [image]
