"""
JWT 生成与解析工具。
"""

import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt

from app.config import get_settings

settings = get_settings()
CLIENT_IDS = frozenset({"gallery_mini", "image_to_prompt_mini"})


def create_access_token(user_id: uuid.UUID, *, client_id: str | None = None) -> str:
    """为指定用户生成 JWT，有效期由配置项 jwt_expire_days 决定。"""
    if client_id is not None and (not isinstance(client_id, str) or client_id not in CLIENT_IDS):
        raise ValueError("Unknown authentication client")
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expire_days),
    }
    if client_id is not None:
        payload = {**payload, "cid": client_id}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _decode_payload(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        uuid.UUID(payload["sub"])
        return payload
    except (JWTError, KeyError, ValueError, TypeError, AttributeError):
        return None


def decode_access_token(token: str) -> uuid.UUID | None:
    """解析 JWT，成功返回用户 ID，失败（过期/签名无效/格式错误）返回 None。"""
    payload = _decode_payload(token)
    return uuid.UUID(payload["sub"]) if payload is not None else None


def decode_access_token_client(token: str) -> str | None:
    """Verify the signed client claim; tokens issued before cid remain legacy."""
    payload = _decode_payload(token)
    if payload is None:
        return None
    if "cid" not in payload:
        return "legacy"
    client_id = payload["cid"]
    return client_id if isinstance(client_id, str) and client_id in CLIENT_IDS else None
