"""
认证相关路由：邮箱验证码登录、Google 登录、获取当前用户信息。

对应 PRD 3.6、后端技术选型.md 3.1 节"用户与认证"接口清单。
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.redis import get_redis_client
from app.schemas.auth import (
    GoogleLoginRequest,
    SendCodeRequest,
    TokenResponse,
    UserOut,
    VerifyCodeRequest,
)
from app.services import auth_service, email_service
from app.utils.deps import get_current_user
from app.utils.google_auth import (
    GoogleTokenInvalidError,
    GoogleVerificationUnavailableError,
    verify_google_id_token,
)
from app.utils.jwt import create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/send-code", status_code=204)
async def send_code(body: SendCodeRequest, redis_client: Redis = Depends(get_redis_client)) -> None:
    """发送邮箱验证码，60 秒限流一次，对应 PRD 3.6 邮箱登录流程第一步。"""
    try:
        await email_service.send_verification_code(redis_client, body.email)
    except email_service.RateLimitedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.post("/verify-code", response_model=TokenResponse)
async def verify_code(
    body: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
    redis_client: Redis = Depends(get_redis_client),
) -> TokenResponse:
    """校验邮箱验证码，成功则登录即注册，返回 JWT。"""
    ok = await email_service.verify_code(redis_client, body.email, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    user = await auth_service.get_or_create_user_by_email(db, body.email)
    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.post("/google", response_model=TokenResponse)
async def google_login(body: GoogleLoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Google 登录：验证前端传来的 id_token，成功则登录即注册，返回 JWT。"""
    try:
        payload = await asyncio.wait_for(asyncio.to_thread(verify_google_id_token, body.id_token), timeout=12)
    except (GoogleVerificationUnavailableError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503, detail="Google 登录验证服务暂不可用，请稍后重试或使用邮箱登录"
        ) from exc
    except GoogleTokenInvalidError as exc:
        raise HTTPException(status_code=400, detail="Google 登录凭证无效") from exc

    user = await auth_service.get_or_create_user_by_google(
        db,
        google_id=payload["sub"],
        email=payload.get("email"),
        nickname=payload.get("name"),
        avatar_url=payload.get("picture"),
    )
    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)) -> UserOut:
    """获取当前登录用户信息。"""
    return UserOut.model_validate(current_user)
