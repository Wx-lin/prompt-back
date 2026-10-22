"""Fixed mini-program identities and signed client claims; no WeChat network."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from jose import jwt
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_db
from app.models import User, WeChatIdentity
from app.redis import get_redis_client
from app.routers import auth
from app.services import wechat_auth
from app.utils import jwt as jwt_utils

GALLERY_APP_ID = "wx0123456789abcdef"
IMAGE_APP_ID = "wx1111111111111111"
IMAGE_LOGIN = "/api/auth/wechat/image-to-prompt"


@pytest.fixture
def configured(monkeypatch):
    settings = SimpleNamespace(
        wechat_app_id=GALLERY_APP_ID,
        wechat_app_secret=SecretStr("gallery-test-secret"),
        wechat_image_to_prompt_app_id=IMAGE_APP_ID,
        wechat_image_to_prompt_app_secret=SecretStr("image-test-secret"),
        jwt_secret_key="test-only-login-rate-limit-key",
    )
    monkeypatch.setattr(wechat_auth, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def upstream(monkeypatch):
    state = SimpleNamespace(
        requests=[],
        payload={"openid": "same-openid", "unionid": "website-identity", "session_key": "discarded"},
    )

    async def handle(request):
        state.requests.append(request)
        return httpx.Response(200, json=state.payload)

    monkeypatch.setattr(wechat_auth.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handle))
    return state


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


@pytest.mark.parametrize(
    "path,app_id,secret,client_id",
    [
        ("/api/auth/wechat", GALLERY_APP_ID, "gallery-test-secret", "gallery_mini"),
        (IMAGE_LOGIN, IMAGE_APP_ID, "image-test-secret", "image_to_prompt_mini"),
    ],
)
@pytest.mark.asyncio
async def test_fixed_login_uses_server_credentials_and_signs_client(
    client, configured, upstream, path, app_id, secret, client_id
):
    api, _ = client
    response = await api.post(path, json={"code": "private-code"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"access_token", "token_type"}
    assert dict(upstream.requests[0].url.params) == {
        "appid": app_id,
        "secret": secret,
        "js_code": "private-code",
        "grant_type": "authorization_code",
    }
    token = response.json()["access_token"]
    claims = jwt.get_unverified_claims(token)
    assert claims["cid"] == client_id and "aud" not in claims
    assert jwt_utils.decode_access_token(token)
    assert jwt_utils.decode_access_token_client(token) == client_id
    assert all(value not in response.text for value in (secret, app_id, "discarded", "same-openid"))


@pytest.mark.asyncio
async def test_same_openid_stays_separate_between_apps_and_website(client, sessions, configured, upstream):
    async with sessions() as db:
        website = User(email="existing@example.test", google_id="website-identity")
        db.add(website)
        await db.commit()
        website_id = website.id
    api, _ = client
    gallery = await api.post("/api/auth/wechat", json={"code": "gallery-code"})
    image = await api.post(IMAGE_LOGIN, json={"code": "image-code"})
    repeated = await api.post(IMAGE_LOGIN, json={"code": "next-code"})
    assert image.status_code == repeated.status_code == 200, image.text
    gallery_id = jwt_utils.decode_access_token(gallery.json()["access_token"])
    image_id = jwt_utils.decode_access_token(image.json()["access_token"])
    assert len({website_id, gallery_id, image_id}) == 3
    assert jwt_utils.decode_access_token(repeated.json()["access_token"]) == image_id
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 3
        rows = (await db.scalars(select(WeChatIdentity))).all()
        assert {(row.app_id, row.openid, row.user_id) for row in rows} == {
            (GALLERY_APP_ID, "same-openid", gallery_id),
            (IMAGE_APP_ID, "same-openid", image_id),
        }
        user = await db.get(User, image_id)
        assert user.email is None and user.google_id is None
    me = await api.get("/api/auth/me", headers={"Authorization": "Bearer " + image.json()["access_token"]})
    assert me.status_code == 200 and me.json()["id"] == str(image_id)


@pytest.mark.parametrize("missing", ["wechat_app_id", "wechat_app_secret"])
@pytest.mark.asyncio
async def test_new_login_does_not_depend_on_gallery_configuration(client, configured, upstream, missing):
    setattr(configured, missing, "" if missing.endswith("_id") else SecretStr(""))
    api, _ = client
    response = await api.post(IMAGE_LOGIN, json={"code": "private-code"})
    assert response.status_code == 200, response.text
    assert upstream.requests[0].url.params["appid"] == IMAGE_APP_ID


@pytest.mark.parametrize(
    "missing", ["wechat_image_to_prompt_app_id", "wechat_image_to_prompt_app_secret"]
)
@pytest.mark.asyncio
async def test_new_configuration_is_checked_before_redis_and_wechat(client, configured, upstream, missing):
    setattr(configured, missing, "" if missing.endswith("_id") else SecretStr(""))
    api, redis = client
    response = await api.post(IMAGE_LOGIN, json={"code": "private-code"})
    assert response.status_code == 503
    redis.eval.assert_not_called()
    assert not upstream.requests
    gallery = await api.post("/api/auth/wechat", json={"code": "gallery-code"})
    assert gallery.status_code == 200, gallery.text


@pytest.mark.parametrize("field", ["appid", "app_id", "openid", "unionid", "cid", "app_secret"])
@pytest.mark.asyncio
async def test_new_login_rejects_client_identity_and_client_claims(client, configured, upstream, field):
    api, redis = client
    response = await api.post(IMAGE_LOGIN, json={"code": "private-code", field: "attacker"})
    assert response.status_code == 422
    redis.eval.assert_not_called()
    assert not upstream.requests


@pytest.mark.parametrize("failure,status", [(61, 429), (RedisError("private URL"), 503)])
@pytest.mark.asyncio
async def test_new_login_rate_limit_fails_closed(client, configured, upstream, failure, status):
    api, redis = client
    if isinstance(failure, Exception):
        redis.eval.side_effect = failure
    else:
        redis.eval.return_value = failure
    response = await api.post(IMAGE_LOGIN, json={"code": "private-code"})
    assert response.status_code == status
    assert "private" not in response.text
    assert not upstream.requests


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"errcode": 40029}, 400),
        ({"errcode": 40163}, 400),
        ({"errcode": 40226}, 403),
        ({"errcode": 45011}, 429),
        ({"errcode": -1, "errmsg": "private upstream failure"}, 503),
        ({"openid": "invalid value"}, 503),
        ([], 503),
    ],
)
@pytest.mark.asyncio
async def test_new_login_preserves_safe_wechat_errors(
    client, configured, upstream, sessions, payload, status
):
    upstream.payload = payload
    api, _ = client
    response = await api.post(IMAGE_LOGIN, json={"code": "private-code"})
    assert response.status_code == status
    assert "private" not in response.text
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.asyncio
async def test_disabled_image_identity_cannot_login(client, sessions, configured, upstream):
    async with sessions() as db:
        user = await wechat_auth.get_or_create_user(
            db, wechat_auth.VerifiedWeChatIdentity(IMAGE_APP_ID, "same-openid")
        )
        user.is_active = False
        await db.commit()
    api, _ = client
    response = await api.post(IMAGE_LOGIN, json={"code": "private-code"})
    assert response.status_code == 403


def signed_token(**claims):
    settings = jwt_utils.settings
    return jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def test_legacy_jwt_shape_stays_compatible_without_client_or_audience():
    user_id = uuid.uuid4()
    token = jwt_utils.create_access_token(user_id)
    assert set(jwt.get_unverified_claims(token)) == {"sub", "iat", "exp"}
    assert jwt_utils.decode_access_token(token) == user_id
    assert jwt_utils.decode_access_token_client(token) == "legacy"


@pytest.mark.parametrize("client_id", ["gallery_mini", "image_to_prompt_mini"])
def test_client_jwt_round_trip_preserves_user_id(client_id):
    user_id = uuid.uuid4()
    token = jwt_utils.create_access_token(user_id, client_id=client_id)
    assert jwt_utils.decode_access_token(token) == user_id
    assert jwt_utils.decode_access_token_client(token) == client_id
    assert "aud" not in jwt.get_unverified_claims(token)


@pytest.mark.parametrize("client_id", ["", None, 42, [], {}, "attacker"])
def test_invalid_signed_client_claim_is_not_authenticated(client_id):
    token = signed_token(sub=str(uuid.uuid4()), cid=client_id)
    assert jwt_utils.decode_access_token_client(token) is None


@pytest.mark.parametrize("subject", ["bad-uuid", None, 42, {}, []])
def test_client_decoder_requires_valid_user_subject(subject):
    token = signed_token(sub=subject, cid="image_to_prompt_mini")
    assert jwt_utils.decode_access_token_client(token) is None


@pytest.mark.parametrize("token", ["", "not-a-token"])
def test_client_decoder_rejects_malformed_tokens(token):
    assert jwt_utils.decode_access_token_client(token) is None


def test_client_decoder_rejects_missing_subject_expiry_and_invalid_signature():
    user_id = str(uuid.uuid4())
    assert jwt_utils.decode_access_token_client(signed_token(cid="image_to_prompt_mini")) is None
    expired = signed_token(
        sub=user_id, cid="image_to_prompt_mini", exp=datetime.now(UTC) - timedelta(seconds=5)
    )
    assert jwt_utils.decode_access_token_client(expired) is None
    forged = jwt.encode({"sub": user_id, "cid": "image_to_prompt_mini"}, "wrong-key", algorithm="HS256")
    assert jwt_utils.decode_access_token_client(forged) is None


def test_token_creation_rejects_unknown_client_id():
    with pytest.raises(ValueError):
        jwt_utils.create_access_token(uuid.uuid4(), client_id="attacker")
