"""Mini-program authentication: no external network or production database."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database import get_db
from app.models import User, WeChatIdentity
from app.redis import get_redis_client
from app.routers import auth
from app.schemas.auth import WeChatLoginRequest
from app.services import wechat_auth
from app.utils.jwt import decode_access_token

APP_ID = "wx0123456789abcdef"


@pytest.fixture
def configured(monkeypatch):
    settings = SimpleNamespace(
        wechat_app_id=APP_ID,
        wechat_app_secret=SecretStr("test-only-secret"),
        jwt_secret_key="test-only-rate-limit-key",
    )
    monkeypatch.setattr(wechat_auth, "get_settings", lambda: settings)
    return settings


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.__table__.create(sync))
        await conn.run_sync(lambda sync: WeChatIdentity.__table__.create(sync))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(sessions):
    app = FastAPI()
    app.include_router(auth.router)

    async def database():
        async with sessions() as db:
            yield db

    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_redis_client] = lambda: redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        yield api, redis


@pytest.mark.parametrize("code", ["", " ", "x" * 513, "bad code", "a\nb", "https://bad/"])
def test_code_validation_rejects_invalid_input(code):
    with pytest.raises(ValidationError):
        WeChatLoginRequest(code=code)


def test_identity_fields_cannot_be_supplied_by_client():
    with pytest.raises(ValidationError):
        WeChatLoginRequest(code="valid-code", openid="attacker")
    assert WeChatLoginRequest(code="  valid-code  ").code == "valid-code"


def test_settings_reject_bad_app_id_and_hide_secret():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, wechat_app_id="not-an-app-id")
    settings = Settings(_env_file=None, wechat_app_id=APP_ID, wechat_app_secret="test-secret")
    assert "test-secret" not in repr(settings)


@pytest.mark.asyncio
async def test_unconfigured_login_fails_before_external_or_database_work(client, configured, monkeypatch):
    configured.wechat_app_secret = SecretStr("")
    external = AsyncMock()
    monkeypatch.setattr(wechat_auth, "_request_code_session", external)
    api, redis = client
    response = await api.post("/api/auth/wechat", json={"code": "valid-code"})
    assert response.status_code == 503
    external.assert_not_called()
    redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_real_transport_uses_fixed_https_without_logging_credentials(configured, monkeypatch, caplog):
    captured = []

    async def upstream(request):
        captured.append(request)
        return httpx.Response(200, json={"openid": "only-openid", "session_key": "never-return"})

    monkeypatch.setattr(wechat_auth.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(upstream))
    with caplog.at_level(logging.DEBUG):
        identity = await wechat_auth.exchange_code("private-code")
    request = captured[0]
    assert str(request.url).split("?")[0] == "https://api.weixin.qq.com/sns/jscode2session"
    assert dict(request.url.params) == {
        "appid": APP_ID,
        "secret": "test-only-secret",
        "js_code": "private-code",
        "grant_type": "authorization_code",
    }
    assert request.extensions["timeout"]["read"] <= 8
    assert identity == wechat_auth.VerifiedWeChatIdentity(APP_ID, "only-openid")
    assert "never-return" not in repr(identity)
    assert all(value not in caplog.text for value in ("test-only-secret", "private-code", "never-return"))


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"errcode": 40029, "errmsg": "secret diagnostic"}, 400),
        ({"errcode": 40163}, 400),
        ({"errcode": 40226}, 403),
        ({"errcode": 45011}, 429),
        ({"errcode": -1}, 503),
        ({"errcode": 40013}, 503),
        ({}, 503),
        ({"openid": ""}, 503),
        ({"openid": "x" * 129}, 503),
        ({"openid": "has space"}, 503),
        ({"openid": "bad\nvalue"}, 503),
        ({"openid": "非openid"}, 503),
        ({"openid": 42}, 503),
        ([], 503),
    ],
)
@pytest.mark.asyncio
async def test_upstream_errors_are_safe_and_do_not_create_users(
    client, configured, monkeypatch, payload, status
):
    monkeypatch.setattr(wechat_auth, "_request_code_session", AsyncMock(return_value=payload))
    api, _ = client
    response = await api.post("/api/auth/wechat", json={"code": "valid-code"})
    assert response.status_code == status
    assert "secret diagnostic" not in response.text


@pytest.mark.parametrize("behavior", ["timeout", "http-error", "bad-json"])
@pytest.mark.asyncio
async def test_transport_failure_is_retryable(client, configured, monkeypatch, behavior):
    async def upstream(request):
        if behavior == "timeout":
            raise httpx.ReadTimeout("sensitive transport details", request=request)
        if behavior == "http-error":
            return httpx.Response(502, text="sensitive upstream body")
        return httpx.Response(200, text="not-json")

    monkeypatch.setattr(wechat_auth.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(upstream))
    api, _ = client
    response = await api.post("/api/auth/wechat", json={"code": "valid-code"})
    assert response.status_code == 503
    assert "sensitive" not in response.text


@pytest.mark.asyncio
async def test_rate_limit_is_atomic_private_and_fails_closed(client, configured, monkeypatch):
    api, redis = client
    external = AsyncMock()
    monkeypatch.setattr(wechat_auth, "_request_code_session", external)
    redis.eval.return_value = wechat_auth.LOGIN_LIMIT + 1
    response = await api.post("/api/auth/wechat", json={"code": "valid-code"})
    assert response.status_code == 429
    key = redis.eval.await_args.args[2]
    assert "127.0.0.1" not in key and "valid-code" not in key
    external.assert_not_called()
    redis.eval.side_effect = RedisError("private redis URL")
    response = await api.post("/api/auth/wechat", json={"code": "valid-code"})
    assert response.status_code == 503 and "private" not in response.text


@pytest.mark.asyncio
async def test_login_persists_same_app_identity_and_never_links_website_account(
    client, sessions, configured, monkeypatch
):
    monkeypatch.setattr(
        wechat_auth,
        "_request_code_session",
        AsyncMock(
            return_value={
                "openid": "openid-a",
                "session_key": "never-return",
                "unionid": "ignored-for-binding",
            }
        ),
    )
    async with sessions() as db:
        web_user = User(email="existing@example.test", google_id="ignored-for-binding")
        db.add(web_user)
        await db.commit()
        website_id = web_user.id
    api, _ = client
    first = await api.post("/api/auth/wechat", json={"code": "code-a"})
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert set(first.json()) == {"access_token", "token_type"}
    user_id = decode_access_token(first.json()["access_token"])
    assert user_id and user_id != website_id
    second = await api.post("/api/auth/wechat", json={"code": "code-b"})
    assert decode_access_token(second.json()["access_token"]) == user_id
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 2
        row = await db.scalar(select(WeChatIdentity))
        assert (row.app_id, row.openid, row.user_id) == (APP_ID, "openid-a", user_id)
        user = await db.get(User, user_id)
        assert user.email is None and user.google_id is None and user.last_login_at is not None
    me = await api.get("/api/auth/me", headers={"Authorization": "Bearer " + first.json()["access_token"]})
    assert me.status_code == 200 and me.json()["id"] == str(user_id)


@pytest.mark.asyncio
async def test_same_openid_from_different_apps_has_distinct_user(sessions):
    async with sessions() as db:
        first = await wechat_auth.get_or_create_user(
            db, wechat_auth.VerifiedWeChatIdentity(APP_ID, "same-openid")
        )
        second = await wechat_auth.get_or_create_user(
            db, wechat_auth.VerifiedWeChatIdentity("wx1111111111111111", "same-openid")
        )
        assert first.id != second.id


@pytest.mark.asyncio
async def test_disabled_user_cannot_login_or_use_old_token(client, sessions, configured, monkeypatch):
    identity = wechat_auth.VerifiedWeChatIdentity(APP_ID, "disabled-openid")
    async with sessions() as db:
        user = await wechat_auth.get_or_create_user(db, identity)
        token = auth.create_access_token(user.id)
        user.is_active = False
        await db.commit()
    monkeypatch.setattr(
        wechat_auth, "_request_code_session", AsyncMock(return_value={"openid": identity.openid})
    )
    api, _ = client
    response = await api.post("/api/auth/wechat", json={"code": "code-a"})
    assert response.status_code == 403
    assert (await api.get("/api/auth/me", headers={"Authorization": "Bearer " + token})).status_code == 401


@pytest.mark.asyncio
async def test_unique_conflict_rolls_back_new_user_and_loads_winner(sessions, monkeypatch):
    identity = wechat_auth.VerifiedWeChatIdentity(APP_ID, "racing-openid")
    async with sessions() as db:
        winner = await wechat_auth.get_or_create_user(db, identity)
        winner_id = winner.id
        real_find = wechat_auth._find_user
        calls = 0

        async def miss_then_find(session, verified):
            nonlocal calls
            calls += 1
            return None if calls == 1 else await real_find(session, verified)

        monkeypatch.setattr(wechat_auth, "_find_user", miss_then_find)
        recovered = await wechat_auth.get_or_create_user(db, identity)
        assert recovered.id == winner_id
        assert await db.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_hidden(sessions, monkeypatch):
    async with sessions() as db:
        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=IntegrityError("test", {}, Exception("db"))))
        with pytest.raises(IntegrityError):
            await wechat_auth.get_or_create_user(db, wechat_auth.VerifiedWeChatIdentity(APP_ID, "openid"))


@pytest.mark.asyncio
async def test_unknown_json_fields_rejected_before_authentication(client, configured):
    api, redis = client
    response = await api.post("/api/auth/wechat", json={"code": "valid", "app_id": APP_ID})
    assert response.status_code == 422
    redis.eval.assert_not_called()
