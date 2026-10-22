"""Atomic Redis frequency limits; task quotas remain in PostgreSQL."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.prompt_analysis.errors import AnalysisError

SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


async def frequency(redis, key, maximum, seconds):
    try:
        count = await redis.eval(SCRIPT, 1, f"prompt_analysis:{key}", seconds)
    except Exception:
        raise AnalysisError(503, "LIMITER_UNAVAILABLE", "服务暂不可用，请稍后重试") from None
    if count > maximum:
        raise AnalysisError(429, "RATE_LIMITED", "操作太频繁，请稍后重试")


async def upload_limit(redis, user_id):
    settings = get_settings()
    day = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    await frequency(redis, f"upload:{user_id}", settings.prompt_analysis_upload_hourly_limit, 3600)
    await frequency(redis, f"upload_global:{day}", settings.prompt_analysis_global_upload_daily_limit, 172800)
