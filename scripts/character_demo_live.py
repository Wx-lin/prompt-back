"""Local-only, temporary character demo. Run with: python scripts/character_demo_live.py.

This is not part of the production app. Each successful request may incur a
model charge. The API key stays on the server and no user image is persisted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path

import httpx
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MODEL = "gpt-image-2.5-sunburst"
ENDPOINT = "https://api.openai.com/v1/images/edits"
ALLOWED_ORIGINS = {"http://localhost:3000", "http://127.0.0.1:3000"}
ALLOWED_HOSTS = {"127.0.0.1:3139", "localhost:3139", "[::1]:3139"}
MAX_BODY = 4096
MAX_IMAGE = 8 * 1024 * 1024
MAX_PROVIDER_BODY = ((MAX_IMAGE + 2) // 3) * 4 + 1024 * 1024
MAX_IMAGES = 20
TTL_SECONDS = 20 * 60
MAX_REQUESTS_PER_MINUTE = 8
ROLE_FILES = {
    "都市女性": "character_01_urban_woman.png",
    "都市男性": "character_02_urban_man.png",
    "成熟女性": "character_03_elegant_woman.png",
    "成熟男性": "character_04_mature_man.png",
    "短发女性": "character_05_short_hair_woman.png",
    "中长发男性": "character_06_long_hair_man.png",
    "卷发女性": "character_07_female_character.png",
    "短发男性": "character_08_male_character.png",
    "棕发女性": "character_09_female_character.png",
    "棕发男性": "character_10_male_character.png",
}
ROLE_DIRECTORY = (
    Path(__file__).resolve().parents[2] / "aigc-prompt-frontend" / "public" / "character-demo" / "characters"
)
_images: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_last_requests: list[float] = []
_lock = asyncio.Lock()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: str = Field(min_length=1, max_length=50)
    hair: str = Field(min_length=1, max_length=50)
    clothing: str = Field(min_length=1, max_length=50)
    expression: str = Field(min_length=1, max_length=50)
    previous_id: str | None = Field(default=None, min_length=20, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class DemoError(Exception):
    pass


def _get_key() -> str:
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    return (os.environ.get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()


def _headers(origin: str | None) -> dict[str, str]:
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Vary": "Origin"}
    if origin in ALLOWED_ORIGINS:
        headers.update(
            {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "content-type",
            }
        )
    return headers


@app.middleware("http")
async def guard(request: Request, call_next):
    origin = request.headers.get("origin")
    headers = _headers(origin)
    host = request.headers.get("host", "").lower()
    if (
        not request.client
        or request.client.host not in {"127.0.0.1", "::1"}
        or host not in ALLOWED_HOSTS
        or origin not in ALLOWED_ORIGINS
    ):
        return JSONResponse({"error": "仅限本地演示页面访问"}, status_code=403, headers=headers)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=headers)
    response = await call_next(request)
    for name, value in headers.items():
        response.headers[name] = value
    return response


def _prune(now: float) -> None:
    for key, (created, _, _) in list(_images.items()):
        if now - created > TTL_SECONDS:
            _images.pop(key, None)
    while len(_images) > MAX_IMAGES:
        _images.popitem(last=False)


def _make_payload(data: GenerationRequest, reference: bytes) -> dict:
    text = (
        f"参考第一张图片中的成年角色，保持其脸型和五官特征，生成真实自然的单人全身角色照片，从头到脚完整展示人物和服饰。"
        f"角色：{data.role}；发型：{data.hair}；服装：{data.clothing}；表情：{data.expression}。"
        "根据以上设定调整画面，保留人物身份和画面中未要求修改的内容，不要文字。"
    )
    return {"reference": reference, "prompt": text}


def _validate_png(content: bytes) -> bytes:
    if not content or len(content) > MAX_IMAGE:
        raise DemoError("生成图片大小异常")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG" or image.width * image.height > 20_000_000:
                raise DemoError("生成图片格式异常")
            image.verify()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise DemoError("生成图片格式异常") from exc
    return content


async def _call_provider(payload: dict, key: str) -> bytes:
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as client:
        async with client.stream(
            "POST",
            ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            data={
                "model": MODEL,
                "prompt": payload["prompt"],
                "n": "1",
                "size": "1024x1536",
                "quality": "medium",
                "output_format": "png",
            },
            files={"image[]": ("reference.png", payload["reference"], "image/png")},
        ) as response:
            if response.status_code != 200:
                raise DemoError("模型调用失败，请检查模型权限或账户配置")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_PROVIDER_BODY:
                    raise DemoError("图片服务返回异常")
                body.extend(chunk)
        try:
            result = json.loads(body)
            encoded = result["data"][0]["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
                raise DemoError("图片服务返回异常")
            image = base64.b64decode(encoded, validate=True)
        except (
            KeyError,
            TypeError,
            IndexError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            binascii.Error,
        ) as exc:
            raise DemoError("图片服务返回异常") from exc
    return _validate_png(image)


@app.post("/generate")
async def generate(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        return JSONResponse({"error": "请求格式不正确"}, status_code=415)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "请求过大"}, status_code=413)
    try:
        data = GenerationRequest.model_validate_json(bytes(body))
    except ValidationError:
        return JSONResponse({"error": "请求参数不正确"}, status_code=422)
    now = time.monotonic()
    _prune(now)
    if data.role not in ROLE_FILES:
        return JSONResponse({"error": "角色选项不正确"}, status_code=422)
    previous = None
    if data.previous_id:
        entry = _images.get(data.previous_id)
        if not entry or entry[2] != data.role:
            return JSONResponse({"error": "参考图片已过期，请重新生成"}, status_code=404)
        previous = entry[1]
    else:
        try:
            previous = _validate_png((ROLE_DIRECTORY / ROLE_FILES[data.role]).read_bytes())
        except (OSError, DemoError):
            return JSONResponse({"error": "角色素材不可用"}, status_code=503)
    key = _get_key()
    if not key:
        return JSONResponse({"error": "GPT 图片模型尚未配置，请在本地配置 OPENAI_API_KEY"}, status_code=503)
    if _lock.locked():
        return JSONResponse({"error": "正在生成，请等待完成"}, status_code=429)
    async with _lock:
        _last_requests[:] = [item for item in _last_requests if now - item < 60]
        if len(_last_requests) >= MAX_REQUESTS_PER_MINUTE:
            return JSONResponse({"error": "请求过于频繁，请稍后再试"}, status_code=429)
        _last_requests.append(now)
        try:
            image = await _call_provider(_make_payload(data, previous), key)
        except DemoError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        except (httpx.HTTPError, OSError, TimeoutError):
            return JSONResponse({"error": "模型服务连接失败，请稍后重试"}, status_code=502)
    result_id = secrets.token_urlsafe(24)
    _images[result_id] = (time.monotonic(), image, data.role)
    _prune(time.monotonic())
    return {"id": result_id, "image": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=3139, log_level="warning", access_log=False)
