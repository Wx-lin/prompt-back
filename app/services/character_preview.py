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
from app.services import scf_image_adapter
from app.services.generation_privileges import unlimited_generation_user_ids
from app.services.prompt_analysis.errors import AnalysisError
from app.services.prompt_analysis_images import MAX_IMAGE_BYTES, prepare_analysis_image

_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)
_REFERENCE_VALIDATORS = asyncio.Semaphore(2)
_REFERENCE_LOCK_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 or tonumber(redis.call('GET', KEYS[2]) or '0') >= 20 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', 60)
local count = redis.call('INCR', KEYS[2])
if count == 1 then redis.call('EXPIRE', KEYS[2], 60) end
return 1
"""
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
local unlimited = ARGV[4] == '1'
if redis.call('EXISTS', KEYS[4]) == 1 or cooldown >= 1
   or (not unlimited and (user_count >= tonumber(ARGV[1]) or global_count >= tonumber(ARGV[2]))) then
  return 0
end
redis.call('SET', KEYS[4], ARGV[3], 'EX', 180)
for i = 1, 3 do
  if i ~= 3 or not unlimited then
    local count = redis.call('INCR', KEYS[i])
    if count == 1 then redis.call('EXPIRE', KEYS[i], i == 1 and 30 or 86400) end
  end
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


def _decode_reference_image(encoded: str) -> bytes:
    raw = base64.b64decode(encoded, validate=True)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("invalid reference")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("invalid reference")
    return prepare_analysis_image(raw, mime).image_bytes


async def prepare_reference_image(encoded: str, user_id: str, redis) -> bytes:
    """Validate an image under a per-user lock without consuming generation quota."""
    lock_key = f"character_preview:validating:{user_id}"
    token = uuid.uuid4().hex
    try:
        allowed = await redis.eval(
            _REFERENCE_LOCK_SCRIPT, 2, lock_key, f"character_preview:validation-rate:{user_id}", token,
        )
    except RedisError:
        raise PreviewError(503, "角色预览暂不可用，请稍后再试") from None
    if not allowed:
        raise PreviewError(429, "参考图片处理中，请稍后再试")
    try:
        try:
            await asyncio.wait_for(_REFERENCE_VALIDATORS.acquire(), timeout=0.1)
        except TimeoutError:
            raise PreviewError(503, "参考图片处理繁忙，请稍后再试") from None
        try:
            task = asyncio.create_task(asyncio.to_thread(_decode_reference_image, encoded))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception:
                    logger.warning("Character reference validation failed after request cancellation")
                raise
            except (ValueError, binascii.Error):
                raise AnalysisError(422, "INVALID_REQUEST", "参考图片无效，请重试") from None
        finally:
            _REFERENCE_VALIDATORS.release()
    finally:
        try:
            await redis.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        except RedisError:
            logger.warning("Character reference validation lock release failed")


async def create_preview(
    prompt: str, user_id: str, redis, aspect_ratio: str, reference: bytes | None = None,
) -> str:
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value().strip()
    if not scf_image_adapter.image_provider_available(settings):
        raise PreviewError(503, "角色预览暂未配置，请稍后再试")
    lock_key = f"character_preview:running:{user_id}"
    lock_token = uuid.uuid4().hex
    unlimited = str(user_id) in {str(value) for value in unlimited_generation_user_ids()}
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
            "1" if unlimited else "0",
        )
    except RedisError:
        raise PreviewError(503, "角色预览暂不可用，请稍后再试") from None
    if not allowed:
        raise PreviewError(429, "生成过于频繁或已达到次数限制，请稍后再试")
    try:
        if reference is not None:
            return await generate_image(prompt, key, IMAGE_SIZES[aspect_ratio], reference)
        return await generate_image(prompt, key, IMAGE_SIZES[aspect_ratio])
    finally:
        try:
            await redis.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, lock_token)
        except RedisError:
            # The expiring lock will eventually self-release; do not mask the result.
            logger.warning("Character preview lock release failed")


async def generate_image(prompt: str, api_key: str, size: str, reference: bytes | None = None) -> str:
    """Send one fixed-size image request; never return upstream diagnostics."""
    settings = get_settings()
    if getattr(settings, "image_scf_enabled", False):
        try:
            image = await scf_image_adapter.generate(
                prompt, size, reference, 150, settings, max_output=scf_image_adapter.MAX_PREVIEW_IMAGE,
            )
            return base64.b64encode(image).decode()
        except scf_image_adapter.ScfImageError as exc:
            if exc.code == "provider_timeout":
                raise PreviewError(504, "图片生成超时，请稍后重试") from None
            raise PreviewError(502, "图片生成暂不可用，请稍后重试") from None
    payload = {
        "model": "gpt-image-2",
        "prompt": prompt,
        "size": size,
        "quality": "low",
        "n": 1,
        "output_format": "png",
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = {"connect": 15.0, "read": 150.0, "write": 15.0, "pool": 15.0}
    if reference is not None:
        request = httpx.Request(
            "POST", "https://api.openai.com/v1/images/edits", headers=headers,
            data={"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": "low", "n": "1"},
            files={"image": ("reference.jpg", reference, "image/jpeg")},
            extensions={"timeout": timeout},
        )
    else:
        request = httpx.Request(
            "POST", "https://api.openai.com/v1/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            content=json.dumps(payload).encode(), extensions={"timeout": timeout},
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
