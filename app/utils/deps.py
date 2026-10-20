"""
认证相关的 FastAPI 依赖函数：从请求头解析 JWT，得到当前登录用户。

提供两种依赖：
- get_current_user：要求必须登录，否则抛 401
- get_optional_current_user：允许匿名，登录则返回用户、未登录返回 None
  （用于首页/详情页等匿名可访问、但登录后要显示"是否已点赞/收藏"的场景）
"""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.utils.jwt import decode_access_token

# auto_error=False：没带 token 时不自动抛错，交给上层依赖决定是否允许匿名
_bearer_scheme = HTTPBearer(auto_error=False)


async def _get_user_by_token(
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> User | None:
    """内部辅助函数：从 Authorization 头解析出用户，解析失败返回 None。"""
    if credentials is None:
        return None
    user_id = decode_access_token(credentials.credentials)
    if user_id is None:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    return user if user is not None and user.is_active else None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """要求必须登录的依赖，常用于点赞/收藏等写操作接口。"""
    user = await _get_user_by_token(credentials, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录已过期")
    return user


async def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """允许匿名访问的依赖，常用于详情接口（需要知道当前用户是否已点赞/收藏）。"""
    return await _get_user_by_token(credentials, db)


def current_user_id_or_none(user: User | None) -> uuid.UUID | None:
    """小工具：从可选用户对象中安全取出 user_id，避免每处都写 `user.id if user else None`。"""
    return user.id if user else None
