"""Public feature visibility and authenticated controls for the mini-program."""

import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.miniprogram_features import MiniProgramFeatures
from app.redis import get_redis_client
from app.utils.admin_auth import verify_admin_key

router = APIRouter()

_RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


async def enforce_feature_limit(request: Request, redis, bucket: str) -> None:
    peer = request.client.host if request.client else "unknown"
    digest = hmac.new(get_settings().jwt_secret_key.encode(), peer.encode(), hashlib.sha256).hexdigest()
    try:
        count = await redis.eval(_RATE_SCRIPT, 1, f"miniprogram_features:{bucket}:{digest}", 60)
    except Exception:
        raise HTTPException(
            503, "功能配置暂不可用，请稍后重试", headers={"Cache-Control": "no-store"}
        ) from None
    if count > (30 if bucket == "admin" else 120):
        raise HTTPException(429, "请求过于频繁，请稍后重试", headers={"Cache-Control": "no-store"})


async def public_limit(request: Request, redis=Depends(get_redis_client)) -> None:
    await enforce_feature_limit(request, redis, "public")


async def admin_limit(request: Request, redis=Depends(get_redis_client)) -> None:
    await enforce_feature_limit(request, redis, "admin")


class FeatureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creation_enabled: StrictBool


def no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


async def read_features(db: AsyncSession) -> FeatureSettings:
    settings = await db.get(MiniProgramFeatures, 1)
    return FeatureSettings(creation_enabled=settings.creation_enabled if settings else False)


@router.get("/api/miniprogram/features", response_model=FeatureSettings, dependencies=[Depends(public_limit)])
async def public_features(response: Response, db: AsyncSession = Depends(get_db)) -> FeatureSettings:
    no_cache(response)
    return await read_features(db)


@router.get(
    "/api/admin/miniprogram/features",
    response_model=FeatureSettings,
    dependencies=[Depends(admin_limit), Depends(verify_admin_key)],
)
async def admin_features(response: Response, db: AsyncSession = Depends(get_db)) -> FeatureSettings:
    no_cache(response)
    return await read_features(db)


@router.put(
    "/api/admin/miniprogram/features",
    response_model=FeatureSettings,
    dependencies=[Depends(admin_limit), Depends(verify_admin_key)],
)
async def update_features(
    payload: FeatureSettings, response: Response, db: AsyncSession = Depends(get_db)
) -> FeatureSettings:
    no_cache(response)
    result = await db.execute(
        update(MiniProgramFeatures)
        .where(MiniProgramFeatures.id == 1)
        .values(creation_enabled=payload.creation_enabled)
    )
    if result.rowcount != 1:
        raise HTTPException(503, "小程序功能配置未初始化")
    await db.commit()
    return payload
