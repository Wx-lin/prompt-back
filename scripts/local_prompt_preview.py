"""Opt-in local creative API. Production app never imports this module.

Run only through the local launcher, or explicitly:
  AIGC_LOCAL_PROMPT_PREVIEW=1 ... python -m uvicorn \
    scripts.local_prompt_preview:create_app --factory --host 127.0.0.1 --port 8040

The public gallery and login stay on the real backend. New private tool rows live
in the separate local preview DB. No scheduler, auth bypass, fake result or model
transport is installed here; the ordinary API and worker implement creation.
"""

import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.database import get_db
from app.redis import get_redis_client
from app.routers import prompt_analyses, prompt_conversations
from app.schemas.prompt_conversation import PromptAssetImport
from app.utils.deps import get_current_user
from app.utils.prompt_analysis_http import PromptAnalysisBoundary, PromptAnalysisRoute, error_response
from scripts.local_prompt_preview_source import PreviewSource, mirror_public_prompt, mirror_user

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
HOSTS = frozenset({"127.0.0.1:8040", "localhost:8040", "[::1]:8040"})
ORIGINS = frozenset({"http://localhost:3137", "http://127.0.0.1:3137"})


def validate_environment(settings):
    try:
        database, redis = urlsplit(settings.database_url), urlsplit(settings.redis_url)
        allowed = (
            os.environ.get("AIGC_LOCAL_PROMPT_PREVIEW") == "1"
            and settings.app_env != "production"
            and database.scheme == "postgresql+asyncpg"
            and database.hostname in LOOPBACK
            and re.fullmatch(r"/aigc_prompt_preview_[A-Za-z0-9_]{1,80}", database.path)
            and not database.query
            and not database.fragment
            and redis.scheme == "redis"
            and redis.hostname in LOOPBACK
            and re.fullmatch(r"/(?:[1-9]|1[0-5])", redis.path)
            and not redis.query
            and not redis.fragment
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise RuntimeError("Explicit isolated loopback settings are required for local preview")


class LocalOnlyBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        values = {key.lower(): value.decode("latin1") for key, value in headers}
        client = scope.get("client") or ("", 0)
        duplicates = any(
            sum(key.lower() == name for key, _ in headers) > 1
            for name in (b"host", b"origin", b"authorization")
        )
        if (
            client[0] not in LOOPBACK
            or values.get(b"host") not in HOSTS
            or duplicates
            or (b"origin" in values and values[b"origin"] not in ORIGINS)
        ):
            return await error_response(403, "FORBIDDEN", "仅允许本地预览访问", "local-preview")(
                scope,
                receive,
                send,
            )
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, *, transport=None):
    settings = settings or get_settings()
    validate_environment(settings)
    engine = create_async_engine(
        settings.database_url, echo=False, pool_pre_ping=True, pool_size=3, max_overflow=0
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = PreviewSource(transport=transport)

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            await source.close()
            await engine.dispose()

    app = FastAPI(title="Local prompt creation preview", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(PromptAnalysisBoundary)
    app.add_middleware(LocalOnlyBoundary)

    async def database():
        async with sessions() as db:
            yield db

    async def verified_user(request: Request, db=Depends(get_db)):
        user = await source.current_user(request.headers.get("authorization"))
        return await mirror_user(db, user)

    # Overrides belong to this new local app only, never to app.main.app.
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = verified_user
    local_import = APIRouter(route_class=PromptAnalysisRoute)

    @local_import.post("/api/prompt-assets/from-prompt", status_code=201)
    async def import_public_reference(
        body: PromptAssetImport,
        request: Request,
        user=Depends(get_current_user),
        db=Depends(get_db),
        redis=Depends(get_redis_client),
    ):
        prompt, image = await source.public_prompt(body.prompt_id)
        await mirror_public_prompt(db, prompt, image)
        return await prompt_conversations.import_prompt(body, request, user, db, redis)

    app.include_router(local_import)
    app.include_router(
        APIRouter(
            routes=[
                route
                for route in prompt_conversations.router.routes
                if getattr(route, "path", "") != "/api/prompt-assets/from-prompt"
            ]
        )
    )
    app.include_router(prompt_analyses.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "local_prompt_preview": True}

    return app
