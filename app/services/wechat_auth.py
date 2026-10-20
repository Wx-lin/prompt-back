"""Real wx.login exchange and independent, app-scoped mini-program accounts.

No nickname/avatar/phone permission is requested. Session keys and unionid are
discarded, and website identities are never searched or linked.
"""

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User, WeChatIdentity

CODE_SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
LOGIN_LIMIT = 60
LOGIN_WINDOW_SECONDS = 60
_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class WeChatAuthError(Exception):
    """Public-safe authentication failure; never includes an upstream payload."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class VerifiedWeChatIdentity:
    app_id: str
    openid: str


def require_configuration() -> None:
    settings = get_settings()
    if not settings.wechat_app_id or not settings.wechat_app_secret.get_secret_value():
        raise WeChatAuthError(503, "微信登录暂未配置，请稍后再试")


async def enforce_login_rate_limit(redis: Redis, client_host: str) -> None:
    """Atomic per-peer limit; do not trust caller-supplied forwarding headers."""
    digest = hmac.new(
        get_settings().jwt_secret_key.encode(), client_host.encode(), hashlib.sha256
    ).hexdigest()
    try:
        count = await redis.eval(_RATE_LIMIT_SCRIPT, 1, f"wechat_login_limit:{digest}", LOGIN_WINDOW_SECONDS)
    except RedisError:
        raise WeChatAuthError(503, "微信登录暂不可用，请稍后再试") from None
    if count > LOGIN_LIMIT:
        raise WeChatAuthError(429, "登录请求过于频繁，请稍后再试")


async def _request_code_session(params: dict[str, str]) -> object:
    # Use the transport directly: AsyncClient's INFO request logger prints the
    # full GET query, which would disclose AppSecret and js_code. No redirects,
    # environment proxy discovery, logging of requests or retries are enabled.
    request = httpx.Request(
        "GET",
        CODE_SESSION_URL,
        params=params,
        extensions={"timeout": {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}},
    )
    try:
        async with asyncio.timeout(8):
            async with httpx.AsyncHTTPTransport(retries=0) as transport:
                response = await transport.handle_async_request(request)
                try:
                    await response.aread()
                    if response.status_code != 200:
                        raise WeChatAuthError(503, "微信登录暂不可用，请稍后再试")
                    return response.json()
                finally:
                    await response.aclose()
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise WeChatAuthError(503, "微信登录暂不可用，请稍后再试") from None


async def exchange_code(code: str) -> VerifiedWeChatIdentity:
    require_configuration()
    settings = get_settings()
    payload = await _request_code_session(
        {
            "appid": settings.wechat_app_id,
            "secret": settings.wechat_app_secret.get_secret_value(),
            "js_code": code,
            "grant_type": "authorization_code",
        }
    )
    if not isinstance(payload, dict):
        raise WeChatAuthError(503, "微信登录暂不可用，请稍后再试")
    error = payload.get("errcode", 0)
    if error in (40029, 40163):
        raise WeChatAuthError(400, "微信登录凭证已失效，请重新登录")
    if error == 40226:
        raise WeChatAuthError(403, "当前微信账号暂时无法登录")
    if error == 45011:
        raise WeChatAuthError(429, "微信登录请求过于频繁，请稍后再试")
    openid = payload.get("openid")
    if error or not isinstance(openid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", openid):
        raise WeChatAuthError(503, "微信登录暂不可用，请稍后再试")
    return VerifiedWeChatIdentity(settings.wechat_app_id, openid)


async def _find_user(db: AsyncSession, identity: VerifiedWeChatIdentity) -> User | None:
    return await db.scalar(
        select(User)
        .join(WeChatIdentity, WeChatIdentity.user_id == User.id)
        .where(WeChatIdentity.app_id == identity.app_id, WeChatIdentity.openid == identity.openid)
    )


def _ensure_active(user: User) -> None:
    if user.is_active is False:
        raise WeChatAuthError(403, "当前账号暂时无法登录")


async def get_or_create_user(db: AsyncSession, identity: VerifiedWeChatIdentity) -> User:
    """Create user and identity in one transaction, recovering concurrent signups."""
    user = await _find_user(db, identity)
    try:
        if user is None:
            user = User(nickname="微信用户", is_active=True)
            db.add(user)
            await db.flush()
            db.add(WeChatIdentity(user_id=user.id, app_id=identity.app_id, openid=identity.openid))
        _ensure_active(user)
        user.last_login_at = datetime.now(UTC)
        await db.commit()
    except IntegrityError:
        # Roll back the new user too: a losing signup must leave no orphan row.
        await db.rollback()
        user = await _find_user(db, identity)
        if user is None:
            raise
        _ensure_active(user)
        user.last_login_at = datetime.now(UTC)
        await db.commit()
    await db.refresh(user)
    return user
