"""User-owned WeChat profiles and private, normalized avatar objects.

Old avatar objects are deliberately retained: deleting them during concurrent
updates can break a committed reference. A separate retention-aware maintenance
job may remove unreferenced objects later; uploads never overwrite an old key.
"""

import asyncio
import io
import logging
import re
import uuid
import warnings
from threading import BoundedSemaphore
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from app.config import get_settings
from app.models import User, WeChatIdentity
from app.schemas.auth import UserOut, UserProfileUpdate
from app.services import cos_service

logger = logging.getLogger(__name__)
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_PIXELS = 4_000_000
MAX_PROFILE_BODY = MAX_AVATAR_BYTES + 65536
AVATAR_PREFIX = "cos-avatar://"
_MIME_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
# Compressed images can expand many times in memory; waiting worker threads
# retain only the bounded upload bytes, never multiple decoded image buffers.
_AVATAR_DECODE_SLOT = BoundedSemaphore(1)
_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


async def _bounded_request(request: Request) -> Request:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_PROFILE_BODY:
            raise HTTPException(413, "请求内容过大")
        chunks.append(chunk)
    body = b"".join(chunks)
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


class ProfileRoute(APIRoute):
    """Bound actual bytes before parsing and mark profile errors private too."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            if request.url.path not in {"/api/auth/me", "/api/auth/me/profile"}:
                return await original(request)
            try:
                if request.url.path.endswith("/profile"):
                    request = await _bounded_request(request)
                response = await original(request)
            except StarletteHTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except ClientDisconnect:
                response = JSONResponse({"detail": "请求读取失败"}, status_code=400)
            except Exception as exc:
                logger.warning("Profile request unavailable: %s", type(exc).__name__)
                response = JSONResponse({"detail": "资料服务暂时不可用，请稍后重试"}, status_code=503)
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


def prepare_avatar(body: bytes, content_type: str) -> bytes:
    """Decode allowed static formats, strip metadata, and crop to 256px JPEG."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in {*_MIME_FORMATS, "application/octet-stream"} or not 0 < len(body) <= MAX_AVATAR_BYTES:
        raise ValueError("请上传不超过 2 MiB 的 JPEG、PNG 或 WebP 图片")
    try:
        with _AVATAR_DECODE_SLOT, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                if image.format not in _MIME_FORMATS.values() or (
                    mime != "application/octet-stream" and image.format != _MIME_FORMATS[mime]
                ):
                    raise ValueError("头像格式不匹配")
                if image.width * image.height > MAX_AVATAR_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("请使用尺寸适当的静态头像")
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                rgba = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGBA"), (256, 256))
                clean = Image.new("RGB", (256, 256), "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                clean.save(output, "JPEG", quality=88)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("头像无法读取或尺寸过大") from exc


async def require_wechat_identity(db: AsyncSession, user_id: uuid.UUID) -> None:
    app_id = get_settings().wechat_app_id
    if not app_id:
        raise HTTPException(503, "资料服务暂时不可用，请稍后重试")
    identity = await db.scalar(
        select(WeChatIdentity.id).where(WeChatIdentity.user_id == user_id, WeChatIdentity.app_id == app_id)
    )
    if identity is None:
        raise HTTPException(403, "此功能仅供当前小程序账号使用")


async def enforce_profile_limit(redis, user_id: uuid.UUID) -> None:
    try:
        count = await redis.eval(_LIMIT_SCRIPT, 1, f"user-profile:{user_id}", 60)
    except Exception as exc:
        logger.warning("Profile rate limit unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "资料服务暂时不可用，请稍后重试") from None
    if count > 10:
        raise HTTPException(429, "操作太频繁，请稍后再试")


def _owned_key(reference: str, user_id: uuid.UUID) -> str | None:
    pattern = rf"cos-avatar://(user-avatars/{re.escape(str(user_id))}/[0-9a-f]{{32}}\.jpg)"
    match = re.fullmatch(pattern, reference)
    return match.group(1) if match else None


def _sign_avatar(key: str) -> str:
    url = cos_service._build_client().get_presigned_url(
        Method="GET", Bucket=cos_service.settings.cos_bucket, Key=key, Expired=3600
    )
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid signed avatar URL")
    return url


async def to_user_out(user: User | UserOut) -> UserOut:
    output = UserOut.model_validate(user)
    reference = output.avatar_url or ""
    if not reference.startswith(AVATAR_PREFIX):
        return output
    key = _owned_key(reference, user.id)
    try:
        url = await asyncio.to_thread(_sign_avatar, key) if key else None
    except Exception as exc:
        logger.warning("Avatar signing unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "头像暂时不可用，请稍后重试") from None
    return output.model_copy(update={"avatar_url": url})


async def _cleanup_unreferenced(db: AsyncSession, key: str) -> None:
    try:
        await db.rollback()
        referenced = await db.scalar(select(User.id).where(User.avatar_url == AVATAR_PREFIX + key).limit(1))
        if referenced is None:
            await asyncio.to_thread(
                cos_service._build_client().delete_object, Bucket=cos_service.settings.cos_bucket, Key=key
            )
    except Exception as exc:
        # Uncertain commit outcome or unavailable DB: keep the object rather than
        # delete a possibly committed avatar. Only random unreferenced keys qualify.
        logger.warning("Avatar cleanup deferred: %s", type(exc).__name__)


async def save_profile(db: AsyncSession, user: User, nickname: str, avatar: UploadFile | None) -> UserOut:
    key = None
    user_id = user.id
    try:
        changes = {"nickname": nickname}
        if avatar is not None:
            try:
                body = await avatar.read(MAX_AVATAR_BYTES + 1)
                clean = await asyncio.to_thread(prepare_avatar, body, avatar.content_type or "")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            key = f"user-avatars/{user_id}/{uuid.uuid4().hex}.jpg"
            await asyncio.to_thread(
                cos_service._build_client().put_object,
                Bucket=cos_service.settings.cos_bucket,
                Body=clean,
                Key=key,
                ContentType="image/jpeg",
                ACL="private",
            )
            changes = {**changes, "avatar_url": AVATAR_PREFIX + key}
        result = await db.execute(
            update(User)
            .where(User.id == user_id, User.is_active.is_(True))
            .values(**changes)
            .returning(User.id, User.email, User.nickname, User.avatar_url, User.created_at)
            .execution_options(synchronize_session=False)
        )
        record = result.mappings().one_or_none()
        if record is None:
            raise HTTPException(403, "账号暂时不可用")
        output = await to_user_out(UserOut.model_validate(record))
        await db.commit()
        return output
    except Exception as exc:
        if key:
            await _cleanup_unreferenced(db, key)
        else:
            await db.rollback()
        if isinstance(exc, HTTPException):
            raise
        logger.warning("Profile save unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "资料保存暂时不可用，请稍后重试") from None


async def update_from_form(request: Request, db: AsyncSession, user: User, redis) -> UserOut:
    await require_wechat_identity(db, user.id)
    await enforce_profile_limit(redis, user.id)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        raise HTTPException(422, "请使用表单提交昵称和头像")
    async with request.form(max_files=1, max_fields=8, max_part_size=4096) as form:
        values = list(form.multi_items())
        if len(values) != len(form):
            raise HTTPException(422, "资料字段不能重复")
        avatar = form.get("avatar")
        if avatar is not None and not isinstance(avatar, UploadFile):
            raise HTTPException(422, "头像必须通过文件上传")
        try:
            fields = UserProfileUpdate.model_validate(
                {key: value for key, value in values if key != "avatar"}
            )
        except ValidationError:
            raise HTTPException(422, "请填写 1–30 个字符的昵称，且不要包含控制字符或其他字段") from None
        return await save_profile(db, user, fields.nickname, avatar)
