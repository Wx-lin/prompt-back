"""Profile HTTP flows with real isolated SQLite transactions; only COS/Redis are fake."""

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Update

from app.config import get_settings
from app.database import get_db
from app.models import User, WeChatIdentity
from app.redis import get_redis_client
from app.routers import auth
from app.services import cos_service, wechat_auth
from app.utils.jwt import create_access_token

APP_ID = "wx0123456789abcdef"
PROFILE = "/api/auth/me/profile"


def picture(format="PNG", size=(30, 50)):
    output = io.BytesIO()
    image = Image.new("RGB", size, "red")
    image.save(output, format=format, exif=Image.Exif())
    return output.getvalue()


class FakeCos:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.deletes = []
        self.signs = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs["Key"])
        self.objects.pop(kwargs["Key"], None)

    def get_presigned_url(self, **kwargs):
        self.signs.append(kwargs)
        return (
            "https://profile-test-bucket.cos.ap-guangzhou.myqcloud.com/" + kwargs["Key"] + "?signature=test"
        )


@pytest_asyncio.fixture
async def profile_client(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: User.__table__.create(sync))
        await connection.run_sync(lambda sync: WeChatIdentity.__table__.create(sync))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        own = User(nickname="微信用户", is_active=True)
        other = User(nickname="另一个用户", is_active=True)
        website = User(nickname="网站用户", google_id="website-user", is_active=True)
        inactive = User(nickname="已停用", is_active=False)
        wrong_app = User(nickname="其他小程序", is_active=True)
        db.add_all([own, other, website, inactive, wrong_app])
        await db.flush()
        db.add_all(
            [
                WeChatIdentity(user_id=own.id, app_id=APP_ID, openid="own-openid"),
                WeChatIdentity(user_id=other.id, app_id=APP_ID, openid="other-openid"),
                WeChatIdentity(user_id=inactive.id, app_id=APP_ID, openid="inactive-openid"),
                WeChatIdentity(user_id=wrong_app.id, app_id="wx1111111111111111", openid="wrong-app-openid"),
            ]
        )
        await db.commit()
        ids = {name: user.id for name, user in locals().copy().items() if isinstance(user, User)}
    monkeypatch.setattr(get_settings(), "wechat_app_id", APP_ID)
    monkeypatch.setattr(cos_service.settings, "cos_bucket", "profile-test-bucket")
    cos = FakeCos()
    monkeypatch.setattr(cos_service, "_build_client", lambda: cos)
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    app = FastAPI()
    app.include_router(auth.router)

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_redis_client] = lambda: redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        yield SimpleNamespace(api=api, sessions=sessions, ids=ids, cos=cos, redis=redis)
    await engine.dispose()


def authorization(context, name="own"):
    return {"Authorization": "Bearer " + create_access_token(context.ids[name])}


@pytest.mark.asyncio
async def test_nickname_only_form_updates_current_user_and_me(profile_client):
    context = profile_client
    response = await context.api.post(
        PROFILE, data={"nickname": "  小明🌟  "}, headers=authorization(context)
    )
    assert response.status_code == 200, response.text
    assert response.json()["nickname"] == "小明🌟"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["avatar_url"] is None
    me = await context.api.get("/api/auth/me", headers=authorization(context))
    assert me.json() == response.json()
    assert me.headers["cache-control"] == "no-store"
    async with context.sessions() as db:
        assert (await db.get(User, context.ids["own"])).nickname == "小明🌟"
        assert (await db.get(User, context.ids["other"])).nickname == "另一个用户"
    assert not context.cos.puts


@pytest.mark.asyncio
async def test_private_avatar_round_trip_survives_login_and_only_signs_owner(profile_client, monkeypatch):
    context = profile_client
    response = await context.api.post(
        PROFILE,
        data={"nickname": "新昵称"},
        files={"avatar": ("anything.png", picture(), "image/png")},
        headers=authorization(context),
    )
    assert response.status_code == 200, response.text
    url = response.json()["avatar_url"]
    assert url.startswith("https://profile-test-bucket.cos.")
    assert "cos-avatar://" not in response.text
    upload = context.cos.puts[0]
    assert upload["ACL"] == "private" and upload["ContentType"] == "image/jpeg"
    key = upload["Key"]
    assert key.startswith(f"user-avatars/{context.ids['own']}/") and key.endswith(".jpg")
    with Image.open(io.BytesIO(upload["Body"])) as avatar:
        assert avatar.size == (256, 256) and avatar.format == "JPEG"
        assert not avatar.getexif() and not avatar.info.get("icc_profile")
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.avatar_url == "cos-avatar://" + key
        identity = wechat_auth.VerifiedWeChatIdentity(APP_ID, "own-openid")
        await wechat_auth.get_or_create_user(db, identity)
    me = await context.api.get("/api/auth/me", headers=authorization(context))
    assert me.json()["nickname"] == "新昵称" and me.json()["avatar_url"] == url
    assert context.cos.signs[-1]["Expired"] == 3600
    assert context.cos.signs[-1]["Key"] == key
    assert not context.cos.deletes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,status", [(None, 401), ("inactive", 401), ("website", 403), ("wrong_app", 403)]
)
async def test_profile_requires_active_identity_for_configured_app(profile_client, name, status):
    context = profile_client
    response = await context.api.post(
        PROFILE,
        data={"nickname": "新名字"},
        headers=authorization(context, name) if name else {},
    )
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert not context.cos.puts and context.redis.eval.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"nickname": ""},
        {"nickname": "  "},
        {"nickname": "名" * 31},
        {"nickname": "有\n换行"},
        {"nickname": "\t名字"},
        {"nickname": "名\u202e字"},
        {"nickname": "名字", "user_id": str(uuid.uuid4())},
        {"nickname": "名字", "openid": "foreign"},
        {"nickname": "名字", "avatar_url": "https://evil.test/avatar.jpg"},
        {"nickname": "名字", "avatar": "wxfile://temporary"},
    ],
)
async def test_invalid_or_server_owned_form_fields_are_rejected(profile_client, fields):
    context = profile_client
    response = await context.api.post(PROFILE, data=fields, headers=authorization(context))
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"
    async with context.sessions() as db:
        assert (await db.get(User, context.ids["own"])).nickname == "微信用户"
    assert not context.cos.puts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,mime",
    [
        (b"not an image", "image/png"),
        (picture(), "image/jpeg"),
        (picture("GIF"), "image/gif"),
        (picture(), "image/svg+xml"),
        (b"x" * (2 * 1024 * 1024 + 1), "image/jpeg"),
        (picture(size=(5000, 5000)), "image/png"),
    ],
    ids=["bad-bytes", "wrong-mime", "gif", "svg", "oversize", "too-many-pixels"],
)
async def test_avatar_rejects_bad_bytes_mime_or_limits(profile_client, body, mime):
    context = profile_client
    response = await context.api.post(
        PROFILE,
        data={"nickname": "名字"},
        files={"avatar": ("avatar.jpg", body, mime)},
        headers=authorization(context),
    )
    assert response.status_code == 422, response.text
    assert not context.cos.puts
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.nickname == "微信用户" and user.avatar_url is None


@pytest.mark.asyncio
async def test_profile_rate_limit_and_redis_failure_fail_closed(profile_client):
    context = profile_client
    context.redis.eval.return_value = 11
    response = await context.api.post(PROFILE, data={"nickname": "名字"}, headers=authorization(context))
    assert response.status_code == 429
    assert context.redis.eval.await_args.args[2] == f"user-profile:{context.ids['own']}"
    context.redis.eval.side_effect = ConnectionError("private Redis credentials")
    response = await context.api.post(PROFILE, data={"nickname": "名字"}, headers=authorization(context))
    assert response.status_code == 503 and "private" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert not context.cos.puts


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["put_object", "get_presigned_url"])
async def test_cos_failure_never_changes_profile_or_leaks_details(profile_client, monkeypatch, operation):
    context = profile_client

    def fail(**kwargs):
        if operation == "put_object":
            context.cos.objects[kwargs["Key"]] = kwargs["Body"]
        raise RuntimeError("private COS credentials")

    monkeypatch.setattr(context.cos, operation, fail)
    response = await context.api.post(
        PROFILE,
        data={"nickname": "名字"},
        files={"avatar": ("avatar.png", picture(), "image/png")},
        headers=authorization(context),
    )
    assert response.status_code == 503 and "private" not in response.text
    assert not context.cos.objects
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.nickname == "微信用户" and user.avatar_url is None


@pytest.mark.asyncio
async def test_database_commit_failure_rolls_back_profile_and_deletes_only_new_object(
    profile_client, monkeypatch
):
    context = profile_client
    old_key = f"user-avatars/{context.ids['own']}/{uuid.uuid4().hex}.jpg"
    context.cos.objects[old_key] = b"old"
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        user.avatar_url = "cos-avatar://" + old_key
        await db.commit()

    async def fail_commit(self):
        raise RuntimeError("private database URL")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    response = await context.api.post(
        PROFILE,
        data={"nickname": "名字"},
        files={"avatar": ("avatar.png", picture(), "image/png")},
        headers=authorization(context),
    )
    assert response.status_code == 503 and "private" not in response.text
    assert context.cos.objects == {old_key: b"old"}
    assert old_key not in context.cos.deletes
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.nickname == "微信用户" and user.avatar_url == "cos-avatar://" + old_key


@pytest.mark.asyncio
async def test_signing_never_accepts_foreign_keys_or_changes_google_avatar(profile_client):
    context = profile_client
    foreign = f"user-avatars/{context.ids['other']}/{uuid.uuid4().hex}.jpg"
    async with context.sessions() as db:
        own = await db.get(User, context.ids["own"])
        own.avatar_url = "cos-avatar://" + foreign
        website = await db.get(User, context.ids["website"])
        website.avatar_url = "https://google.example.test/avatar.jpg"
        await db.commit()
    me = await context.api.get("/api/auth/me", headers=authorization(context))
    assert me.status_code == 200 and me.json()["avatar_url"] is None
    assert not context.cos.signs
    website = await context.api.get("/api/auth/me", headers=authorization(context, "website"))
    assert website.json()["avatar_url"] == "https://google.example.test/avatar.jpg"


@pytest.mark.asyncio
async def test_direct_api_bounds_chunked_request_before_form_parser(profile_client):
    context = profile_client

    async def too_large():
        for _ in range(4):
            yield b"x" * 1024 * 1024

    response = await context.api.post(
        PROFILE,
        content=too_large(),
        headers={**authorization(context), "Content-Type": "multipart/form-data; boundary=test"},
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert not context.cos.puts


@pytest.mark.asyncio
async def test_nickname_edit_preserves_concurrently_saved_avatar(profile_client, monkeypatch):
    context = profile_client
    concurrent_key = f"user-avatars/{context.ids['own']}/{uuid.uuid4().hex}.jpg"
    original_execute = AsyncSession.execute
    changed = False

    async def interleaved_execute(self, statement, *args, **kwargs):
        nonlocal changed
        if isinstance(statement, Update) and not changed:
            changed = True
            async with context.sessions() as other:
                await original_execute(
                    other,
                    update(User)
                    .where(User.id == context.ids["own"])
                    .values(avatar_url="cos-avatar://" + concurrent_key),
                )
                await other.commit()
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", interleaved_execute)
    response = await context.api.post(PROFILE, data={"nickname": "并发名字"}, headers=authorization(context))
    assert response.status_code == 200, response.text
    assert concurrent_key in response.json()["avatar_url"]
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.nickname == "并发名字" and user.avatar_url == "cos-avatar://" + concurrent_key


@pytest.mark.asyncio
async def test_uncertain_commit_never_deletes_a_committed_avatar(profile_client, monkeypatch):
    context = profile_client
    original_commit = AsyncSession.commit

    async def ambiguous_commit(self):
        await original_commit(self)
        raise ConnectionError("lost commit acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", ambiguous_commit)
    response = await context.api.post(
        PROFILE,
        data={"nickname": "已提交"},
        files={"avatar": ("avatar.png", picture(), "image/png")},
        headers=authorization(context),
    )
    assert response.status_code == 503
    assert not context.cos.deletes
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.nickname == "已提交"
        assert user.avatar_url.removeprefix("cos-avatar://") in context.cos.objects


@pytest.mark.asyncio
async def test_identity_database_failure_is_sanitized_and_private(profile_client, monkeypatch):
    context = profile_client
    original_scalar = AsyncSession.scalar

    async def failed_scalar(self, statement, *args, **kwargs):
        if "wechat_identities" in str(statement):
            raise ConnectionError("postgres://private-password@host")
        return await original_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", failed_scalar)
    response = await context.api.post(PROFILE, data={"nickname": "名字"}, headers=authorization(context))
    assert response.status_code == 503
    assert "private" not in response.text and response.headers["cache-control"] == "no-store"
