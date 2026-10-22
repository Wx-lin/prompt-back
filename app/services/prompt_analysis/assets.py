"""Private upload ownership, bounded normalization and preview renewal."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select

from app.config import get_settings
from app.models import PromptAnalysis, PromptAnalysisAsset
from app.services.prompt_analysis import limits, storage
from app.services.prompt_analysis.errors import AnalysisError
from app.services.prompt_analysis_images import prepare_analysis_image

_decode_lock = asyncio.Lock()
MAX_FILE_BYTES = 5 * 1024 * 1024


def ensure_enabled():
    if not get_settings().prompt_analysis_enabled:
        raise AnalysisError(503, "FEATURE_UNAVAILABLE", "图片转提示词暂未开放")


def not_found():
    return AnalysisError(404, "NOT_FOUND", "记录不存在或已过期")


async def is_available(db, asset, now=None):
    now = now or datetime.now(UTC)
    if asset.state != "ready":
        return False
    if asset.unbound_expires_at > now:
        # A manual deletion marks an otherwise unreferenced asset deleting.
        return True
    return bool(
        await db.scalar(
            select(
                exists().where(
                    PromptAnalysis.asset_id == asset.id,
                    PromptAnalysis.deleted_at.is_(None),
                    or_(
                        PromptAnalysis.image_retain_until > now,
                        PromptAnalysis.status.in_(["queued", "running"]),
                    ),
                )
            )
        )
    )


async def owned_asset(db, user_id, asset_id, *, lock=False):
    query = select(PromptAnalysisAsset).where(
        PromptAnalysisAsset.id == asset_id,
        PromptAnalysisAsset.user_id == user_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    asset = await db.scalar(query)
    if asset is None or not await is_available(db, asset):
        raise not_found()
    return asset


async def preview_fields(asset):
    now = datetime.now(UTC)
    try:
        image, thumb = await asyncio.gather(
            asyncio.to_thread(storage.preview_url, asset.object_key),
            asyncio.to_thread(storage.preview_url, asset.thumbnail_key),
        )
    except Exception:
        raise AnalysisError(503, "STORAGE_UNAVAILABLE", "图片预览暂不可用") from None
    return {
        "preview_url": image,
        "thumbnail_url": thumb,
        "preview_expires_at": (now + timedelta(seconds=600)).isoformat(),
    }


async def asset_data(asset):
    return {
        "id": str(asset.id),
        "width": asset.width,
        "height": asset.height,
        "byte_size": asset.byte_size,
        **await preview_fields(asset),
    }


async def upload(db, redis, user_id, file):
    ensure_enabled()
    storage.require_storage_configuration()
    await limits.upload_limit(redis, user_id)
    content_type = (file.content_type or "").split(";")[0].lower()
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise AnalysisError(415, "UNSUPPORTED_IMAGE", "请上传 JPEG、PNG 或 WebP 图片")
    if _decode_lock.locked():
        raise AnalysisError(503, "UPLOAD_BUSY", "图片处理中，请稍后重试")
    async with _decode_lock:
        body = await file.read(MAX_FILE_BYTES + 1)
        if len(body) > MAX_FILE_BYTES:
            raise AnalysisError(413, "PAYLOAD_TOO_LARGE", "图片不能超过 5 MiB")
        try:
            prepared = await asyncio.to_thread(prepare_analysis_image, body, content_type)
        except ValueError:
            raise AnalysisError(422, "INVALID_IMAGE", "图片无法读取、尺寸超限或不是静态图片") from None
    asset_id = uuid.uuid4()
    asset = PromptAnalysisAsset(
        id=asset_id,
        user_id=user_id,
        object_key=f"prompt-analysis/{asset_id.hex}/image.jpg",
        thumbnail_key=f"prompt-analysis/{asset_id.hex}/thumb.jpg",
        sha256=prepared.sha256,
        width=prepared.width,
        height=prepared.height,
        byte_size=len(prepared.image_bytes),
        state="uploading",
        unbound_expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db.add(asset)
    object_key, thumbnail_key = asset.object_key, asset.thumbnail_key
    await db.commit()  # Cleanup can discover partial uploads after process loss.
    try:
        await storage.put_private(prepared.image_bytes, object_key)
        await storage.put_private(prepared.thumbnail_bytes, thumbnail_key)
        response = await asset_data(asset)
        asset.state = "ready"
        await db.commit()
        return response
    except Exception:
        await db.rollback()
        await asyncio.gather(
            storage.delete_private(object_key),
            storage.delete_private(thumbnail_key),
            return_exceptions=True,
        )
        # The durable uploading record remains discoverable if compensation fails.
        raise AnalysisError(503, "STORAGE_UNAVAILABLE", "图片上传失败，请稍后重试") from None
