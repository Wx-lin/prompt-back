"""
邮箱验证码服务：生成验证码、存入 Redis、调用 Resend 发信、校验。

对应后端技术选型.md 4.1 节"邮箱验证码登录"流程。
"""

import random

import resend
from redis.asyncio import Redis

from app.config import get_settings

_CODE_KEY_PREFIX = "verify:"
_RATE_LIMIT_KEY_PREFIX = "send_limit:"
_CODE_TTL_SECONDS = 10 * 60  # 10 分钟
_RATE_LIMIT_SECONDS = 60  # 同一邮箱 60 秒内只能发一次


class RateLimitedError(Exception):
    """发送频率超限时抛出。"""


def _generate_code() -> str:
    """生成 6 位数字验证码。"""
    return f"{random.randint(0, 999999):06d}"


async def send_verification_code(redis_client: Redis, email: str) -> None:
    """
    生成验证码、存入 Redis（带 TTL）、发送邮件。

    发送前先检查限流 key，防止同一邮箱被频繁刷接口。
    """
    rate_limit_key = f"{_RATE_LIMIT_KEY_PREFIX}{email}"
    if await redis_client.exists(rate_limit_key):
        raise RateLimitedError(f"请求过于频繁，请 {_RATE_LIMIT_SECONDS} 秒后再试")

    code = _generate_code()
    await redis_client.set(f"{_CODE_KEY_PREFIX}{email}", code, ex=_CODE_TTL_SECONDS)
    await redis_client.set(rate_limit_key, "1", ex=_RATE_LIMIT_SECONDS)

    settings = get_settings()
    resend.api_key = settings.resend_api_key
    resend.Emails.send(
        {
            "from": settings.email_from,
            "to": email,
            "subject": "你的 AIGC提示词 登录验证码",
            "html": f"<p>你的验证码是 <strong>{code}</strong>，10 分钟内有效，请勿泄露给他人。</p>",
        }
    )


async def verify_code(redis_client: Redis, email: str, code: str) -> bool:
    """
    校验验证码是否正确。校验成功后立即删除 Redis 中的记录，防止同一验证码被重复使用。
    """
    key = f"{_CODE_KEY_PREFIX}{email}"
    stored_code = await redis_client.get(key)
    if stored_code is None or stored_code != code:
        return False
    await redis_client.delete(key)
    return True
