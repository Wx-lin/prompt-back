"""Server-only image generation for a user-selected character prompt."""

import asyncio
import base64
import binascii
import json
import logging
import uuid

import httpx
from redis.exceptions import RedisError

from app.config import get_settings
from app.services.prompt_analysis.errors import AnalysisError

_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)
IMAGE_SIZES = {
    "1:1": "1024x1024",
    "3:4": "864x1152",
    "4:3": "1152x864",
    "9:16": "864x1536",
    "16:9": "1536x864",
    "21:9": "1344x576",
}
# Fixed windows start on the first request. Check and update all keys atomically.
_RATE_LIMIT_SCRIPT = """
local cooldown = tonumber(redis.call('GET', KEYS[1]) or '0')
local user_count = tonumber(redis.call('GET', KEYS[2]) or '0')
local global_count = tonumber(redis.call('GET', KEYS[3]) or '0')
if redis.call('EXISTS', KEYS[4]) == 1 or cooldown >= 1
   or user_count >= tonumber(ARGV[1]) or global_count >= tonumber(ARGV[2]) then
  return 0
end
redis.call('SET', KEYS[4], ARGV[3], 'EX', 180)
for i = 1, 3 do
  local count = redis.call('INCR', KEYS[i])
  if count == 1 then redis.call('EXPIRE', KEYS[i], i == 1 and 30 or 86400) end
end
return 1
"""
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class PreviewError(AnalysisError):
    def __init__(self, status_code: int, message: str):
        codes = {
            429: "RATE_LIMITED",
            502: "IMAGE_GENERATION_FAILED",
            503: "PREVIEW_UNAVAILABLE",
            504: "IMAGE_GENERATION_TIMEOUT",
        }
        super().__init__(status_code, codes[status_code], message)


async def create_preview(prompt: str, user_id: str, redis, aspect_ratio: str) -> str:
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value().strip()
    if not key:
        raise PreviewError(503, "角色预览暂未配置，请稍后再试")
    lock_key = f"character_preview:running:{user_id}"
    lock_token = uuid.uuid4().hex
    try:
        allowed = await redis.eval(
            _RATE_LIMIT_SCRIPT,
            4,
            f"character_preview:cooldown:{user_id}",
            f"character_preview:user:{user_id}",
            "character_preview:global",
            lock_key,
            getattr(settings, "character_preview_user_daily_limit", 10),
            getattr(settings, "character_preview_global_daily_limit", 100),
            lock_token,
        )
    except RedisError:
        raise PreviewError(503, "角色预览暂不可用，请稍后再试") from None
    if not allowed:
        raise PreviewError(429, "生成过于频繁或已达到次数限制，请稍后再试")
    try:
        return await generate_image(prompt, key, IMAGE_SIZES[aspect_ratio])
    finally:
        try:
            await redis.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, lock_token)
        except RedisError:
            # The expiring lock will eventually self-release; do not mask the result.
            logger.warning("Character preview lock release failed")


async def generate_image(prompt: str, api_key: str, size: str) -> str:
    """Send one fixed-size image request; never return upstream diagnostics."""
    payload = {
        "model": "gpt-image-2",
        "prompt": prompt,
        "size": size,
        "quality": "low",
        "n": 1,
        "output_format": "png",
    }
    request = httpx.Request(
        "POST",
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        content=json.dumps(payload).encode(),
        extensions={"timeout": {"connect": 15.0, "read": 150.0, "write": 15.0, "pool": 15.0}},
    )
    try:
        async with asyncio.timeout(150):
            async with httpx.AsyncHTTPTransport(retries=0) as transport:
                response = await transport.handle_async_request(request)
                try:
                    if response.status_code == 429:
                        raise PreviewError(503, "图片生成服务繁忙或额度不足，请稍后再试")
                    if response.status_code != 200:
                        raise PreviewError(502, "图片生成失败，请检查模型权限或稍后重试")
                    parts = []
                    length = 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > _MAX_RESPONSE_BYTES:
                            raise PreviewError(502, "生成结果过大，请稍后重试")
                        parts.append(chunk)
                    result = json.loads(b"".join(parts))
                    image = result["data"][0]["b64_json"]
                    if not isinstance(image, str) or not image or len(image) > _MAX_RESPONSE_BYTES:
                        raise ValueError("invalid image")
                    decoded = base64.b64decode(image, validate=True)
                    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("unexpected image format")
                    return image
                finally:
                    await response.aclose()
    except (httpx.TimeoutException, TimeoutError):
        raise PreviewError(504, "图片生成超时，请稍后重试") from None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, binascii.Error):
        raise PreviewError(502, "图片生成暂不可用，请稍后重试") from None
