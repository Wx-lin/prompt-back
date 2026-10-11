"""
JWT 生成与解析工具。
"""

import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()


def create_access_token(user_id: uuid.UUID) -> str:
    """为指定用户生成 JWT，有效期由配置项 jwt_expire_days 决定。"""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> uuid.UUID | None:
    """解析 JWT，成功返回用户 ID，失败（过期/签名无效/格式错误）返回 None。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        return None
