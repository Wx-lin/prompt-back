"""Local opt-in browser fixture: real PostgreSQL, simulated COS/Redis/session only.

Run with AIGC_TEST_DATABASE_URL set, bind to 127.0.0.1, never deploy this module.
"""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models import Prompt, PromptImage, User
from app.redis import get_redis_client
from app.routers import admin, admin_submissions, categories, model_catalog, prompts, submissions
from app.services import cos_service
from app.utils.admin_auth import verify_admin_key
from app.utils.deps import get_current_user
from app.utils.submission_body_limit import SubmissionBodyLimit

_db_url = os.environ["AIGC_TEST_DATABASE_URL"]
_schema = "browser_submissions_" + uuid.uuid4().hex
_admin = create_async_engine(_db_url)
_engine = create_async_engine(_db_url, connect_args={"server_settings": {"search_path": _schema}})
_sessions = async_sessionmaker(_engine, expire_on_commit=False)
_objects = {}
_user = None


@asynccontextmanager
async def lifespan(_):
    global _user
    async with _admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{_schema}"'))
    async with _engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with _sessions() as db:
        _user = User(nickname="测试创作者", is_active=True)
        db.add(_user)
        db.add_all(
            [
                Prompt(
                    title="目录测试",
                    prompt_text="test",
                    category="portrait",
                    models=[model],
                    is_published=True,
                )
                for model in ["Midjourney V6", "Midjourney V7"]
            ]
        )
        db.add_all(
            [
                Prompt(
                    id=uuid.UUID(int=100 + index),
                    title=f"精选测试作品 {index:02d}",
                    prompt_text=f"真实数据库测试提示词 {index}",
                    category="portrait" if index % 2 == 0 else "poster",
                    models=["Midjourney V6"],
                    is_published=index < 26,
                    like_count=index,
                    view_count=100 - index,
                    created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index),
                    images=[PromptImage(image_url="http://127.0.0.1:3000/demo/portrait.jpg", thumbnail_url="http://127.0.0.1:3000/demo/portrait.jpg")],
                )
                for index in range(27)
            ]
        )
        await db.commit()
    try:
        yield
    finally:
        await _engine.dispose()
        async with _admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{_schema}" CASCADE'))
        await _admin.dispose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SubmissionBodyLimit)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5176", "http://localhost:5176"],
    allow_methods=["*"],
    allow_headers=["*"],
)
for router in (
    submissions.router,
    admin_submissions.router,
    prompts.router,
    categories.router,
    model_catalog.router,
    admin.router,
):
    app.include_router(router)


async def db():
    async with _sessions() as session:
        yield session


async def user(request: Request):
    if request.headers.get("authorization") != "Bearer test-session":
        raise HTTPException(401)
    return _user


async def admin_key(request: Request):
    if request.headers.get("X-Admin-Key") != "test-admin":
        raise HTTPException(401)


class Redis:
    async def eval(self, *_):
        return 1


app.dependency_overrides.update(
    {get_db: db, get_current_user: user, get_redis_client: lambda: Redis(), verify_admin_key: admin_key}
)


@app.get("/api/auth/me")
async def me(current=Depends(user)):
    return {
        "id": str(current.id),
        "email": None,
        "nickname": current.nickname,
        "avatar_url": None,
        "created_at": current.created_at.isoformat(),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


async def upload(body, key):
    _objects[key] = [body, False]
    return f"http://127.0.0.1:8100/test-assets/{key}"


def preview(key):
    return f"http://127.0.0.1:8100/test-assets/{key}?preview=test"


async def visible(key, public):
    _objects[key][1] = public


async def delete(key):
    _objects.pop(key, None)


cos_service.upload_submission_image = upload
cos_service.submission_preview_url = preview
cos_service.set_submission_visibility = visible
cos_service.delete_submission_image = delete


@app.get("/test-assets/{key:path}")
async def asset(key: str, preview: str | None = None):
    value = _objects.get(key)
    if value is None or (not value[1] and preview != "test"):
        raise HTTPException(404)
    return Response(value[0], media_type="image/jpeg")
