"""One bounded OpenAI image generation or image-edit request, with no retries."""

import asyncio
import base64
import binascii
import json

import httpx

from app.config import get_settings
from app.services import scf_image_adapter
from app.services.vision.adapter import VisionError

MODEL = "gpt-image-2"
PROMPT_VERSION = "image-1"
MAX_RESPONSE = 20 * 1024 * 1024
MAX_IMAGE = 5 * 1024 * 1024


def build_image_prompt(message: str, context: dict | None) -> str:
    """Carry bounded prior conversation as context, with the current request last."""
    if not context:
        return message
    parts = []
    prompt = context.get("prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("zh"), str):
        parts.append("先前整理的画面描述：" + prompt["zh"][:3000])
    dialogue = context.get("recent_dialogue")
    if isinstance(dialogue, list):
        for item in dialogue[-3:]:
            if isinstance(item, dict):
                user = item.get("user")
                assistant = item.get("assistant")
                if isinstance(user, str) and user:
                    parts.append("之前的用户描述：" + user[:500])
                if isinstance(assistant, str) and assistant:
                    parts.append("之前的回复：" + assistant[:500])
    if not parts:
        return message
    return "以下内容仅供理解上下文：\n" + "\n".join(parts) + "\n本次生成要求（优先）：" + message


async def generate(prompt: str, *, api_key: str, reference: bytes | None, timeout: float) -> bytes:
    settings = get_settings()
    if getattr(settings, "image_scf_enabled", False):
        try:
            return await scf_image_adapter.generate(prompt, "1024x1024", reference, timeout, settings)
        except scf_image_adapter.ScfImageError as exc:
            raise VisionError(exc.code, uncertain=exc.uncertain) from None
    fields = {"model": MODEL, "prompt": prompt, "size": "1024x1024", "quality": "low", "n": "1"}
    headers = {"Authorization": f"Bearer {api_key}"}
    if reference:
        is_png = reference.startswith(b"\x89PNG\r\n\x1a\n")
        request = httpx.Request(
            "POST", "https://api.openai.com/v1/images/edits", headers=headers,
            data=fields, files={"image": (
                "reference.png" if is_png else "reference.jpg", reference,
                "image/png" if is_png else "image/jpeg",
            )},
        )
    else:
        request = httpx.Request(
            "POST", "https://api.openai.com/v1/images/generations", headers=headers,
            json={**fields, "n": 1, "output_format": "png"},
        )
    request.extensions["timeout"] = {k: timeout for k in ("connect", "read", "write", "pool")}
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncHTTPTransport(retries=0) as transport:
                response = await transport.handle_async_request(request)
                try:
                    if response.status_code != 200:
                        raise VisionError("image_provider_error", uncertain=response.status_code >= 500)
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > MAX_RESPONSE:
                            raise VisionError("image_invalid_output")
                        chunks.append(chunk)
                    encoded = json.loads(b"".join(chunks))["data"][0]["b64_json"]
                    if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE:
                        raise ValueError("invalid result")
                    body = base64.b64decode(encoded, validate=True)
                    if not body.startswith(b"\x89PNG\r\n\x1a\n") or len(body) > MAX_IMAGE:
                        raise ValueError("invalid image")
                    return body
                finally:
                    await response.aclose()
    except (TimeoutError, httpx.TimeoutException):
        raise VisionError("provider_timeout", uncertain=True) from None
    except httpx.HTTPError:
        raise VisionError("image_transport_error", uncertain=True) from None
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error):
        raise VisionError("image_invalid_output") from None
