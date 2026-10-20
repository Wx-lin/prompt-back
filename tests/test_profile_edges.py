"""Profile failure boundaries and cleanup safety, using the same isolated fixture."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User
from tests.test_user_profile import PROFILE, authorization, picture

pytest_plugins = ["tests.test_user_profile"]


@pytest.mark.asyncio
async def test_duplicate_fields_json_and_missing_config_are_rejected(profile_client, monkeypatch):
    context = profile_client
    duplicate = await context.api.post(
        PROFILE,
        content="nickname=a&nickname=b",
        headers={**authorization(context), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 422
    json = await context.api.post(PROFILE, json={"nickname": "名字"}, headers=authorization(context))
    assert json.status_code == 422 and json.headers["cache-control"] == "no-store"
    monkeypatch.setattr(get_settings(), "wechat_app_id", "")
    unconfigured = await context.api.post(PROFILE, data={"nickname": "名字"}, headers=authorization(context))
    assert unconfigured.status_code == 503


@pytest.mark.asyncio
async def test_existing_avatar_is_signed_on_nickname_only_edit(profile_client):
    context = profile_client
    key = f"user-avatars/{context.ids['own']}/{uuid.uuid4().hex}.jpg"
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        user.avatar_url = "cos-avatar://" + key
        await db.commit()
    response = await context.api.post(PROFILE, data={"nickname": "只改昵称"}, headers=authorization(context))
    assert response.status_code == 200 and key in response.json()["avatar_url"]
    assert not context.cos.puts and not context.cos.deletes


@pytest.mark.asyncio
async def test_insecure_or_failed_signing_never_exposes_marker(profile_client, monkeypatch):
    context = profile_client
    key = f"user-avatars/{context.ids['own']}/{uuid.uuid4().hex}.jpg"
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        user.avatar_url = "cos-avatar://" + key
        await db.commit()
    monkeypatch.setattr(context.cos, "get_presigned_url", lambda **_: "http://insecure.example/avatar")
    response = await context.api.get("/api/auth/me", headers=authorization(context))
    assert response.status_code == 503 and "cos-avatar" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_me_without_authentication_is_not_cacheable(profile_client):
    response = await profile_client.api.get("/api/auth/me")
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_cleanup_uncertainty_retains_new_object_for_later_reconciliation(profile_client, monkeypatch):
    context = profile_client

    async def fail_commit(self):
        raise ConnectionError("unavailable DB")

    original_scalar = AsyncSession.scalar

    async def uncertain_lookup(self, statement, *args, **kwargs):
        if "users.avatar_url" in str(statement):
            raise ConnectionError("DB cannot confirm reference")
        return await original_scalar(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    monkeypatch.setattr(AsyncSession, "scalar", uncertain_lookup)
    response = await context.api.post(
        PROFILE,
        data={"nickname": "名字"},
        files={"avatar": ("avatar.png", picture(), "image/png")},
        headers=authorization(context),
    )
    assert response.status_code == 503
    assert context.cos.objects and not context.cos.deletes
    async with context.sessions() as db:
        user = await db.get(User, context.ids["own"])
        assert user.nickname == "微信用户" and user.avatar_url is None
