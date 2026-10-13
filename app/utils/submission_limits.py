"""Atomic per-user upload/submission limits shared by all API workers."""

from fastapi import HTTPException
from redis.asyncio import Redis

_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


async def enforce_limit(redis: Redis, user_id, action: str, maximum: int, seconds: int):
    try:
        count = await redis.eval(_SCRIPT, 1, f"submission:{action}:{user_id}", seconds)
    except Exception as exc:
        raise HTTPException(503, "提交服务暂时不可用，请稍后再试") from exc
    if count > maximum:
        raise HTTPException(429, "操作太频繁，请稍后再试")
