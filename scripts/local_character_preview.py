"""Opt-in, loopback-only image preview without the other local services.

Run with AIGC_LOCAL_CHARACTER_PREVIEW=1 and an isolated loopback REDIS_URL.
The API key is read from the server environment by the normal image service.
"""

import os
from urllib.parse import urlsplit

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.routers import character_interpret
from app.utils.prompt_analysis_http import PromptAnalysisBoundary, error_response
from scripts.local_unlimited_character_preview import create_local_character_router

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
HOSTS = frozenset({"127.0.0.1:8051", "localhost:8051", "[::1]:8051"})


def validate_environment(settings: Settings) -> None:
    try:
        redis = urlsplit(settings.redis_url)
        allowed = (
            os.environ.get("AIGC_LOCAL_CHARACTER_PREVIEW") == "1"
            and settings.app_env == "local-character-preview"
            and redis.scheme == "redis"
            and redis.hostname in LOOPBACK
            and redis.path == "/12"
            and not redis.username
            and not redis.password
            and not redis.query
            and not redis.fragment
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise RuntimeError("Explicit isolated loopback settings are required for local character preview")


class LocalOnlyBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        hosts = [value.decode("latin1") for key, value in headers if key.lower() == b"host"]
        origin = any(key.lower() == b"origin" for key, _ in headers)
        client = scope.get("client") or ("", 0)
        if client[0] not in LOOPBACK or len(hosts) != 1 or hosts[0] not in HOSTS or origin:
            return await error_response(403, "FORBIDDEN", "仅允许本地预览访问", "local-preview")(
                scope, receive, send
            )
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    validate_environment(settings)
    app = FastAPI(title="Local character preview", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PromptAnalysisBoundary)
    app.add_middleware(LocalOnlyBoundary)
    app.include_router(create_local_character_router(settings))
    app.include_router(character_interpret.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "local_character_preview": True}

    return app
