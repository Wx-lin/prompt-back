"""Authenticated user uploads and manual-review submissions."""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import SubmissionAsset, User
from app.redis import get_redis_client
from app.schemas.submission import SubmissionCreate, SubmissionList, SubmissionOut, SubmissionUploadOut
from app.services import cos_service, submission_service
from app.services.submission_images import MAX_IMAGE_BYTES, prepare_image
from app.utils.deps import get_current_user
from app.utils.submission_limits import enforce_limit

router = APIRouter(prefix="/api/submissions", tags=["submissions"])
logger = logging.getLogger(__name__)


async def active_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_active:
        raise HTTPException(403, "账号不可用")
    return user


@router.post("/upload", response_model=SubmissionUploadOut, status_code=201)
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(active_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
):
    try:
        await enforce_limit(redis, user.id, "upload", 30, 3600)
        body = await file.read(MAX_IMAGE_BYTES + 1)
        try:
            clean, _ = await asyncio.to_thread(prepare_image, body, file.content_type or "")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        asset_id = uuid.uuid4()
        key = f"submissions/{user.id}/{asset_id.hex}.jpg"
        try:
            url = await cos_service.upload_submission_image(clean, key)
        except Exception as exc:
            logger.warning("Submission upload unavailable: %s", type(exc).__name__)
            raise HTTPException(503, "图片上传暂时不可用，请稍后再试") from exc
        asset = SubmissionAsset(id=asset_id, user_id=user.id, object_key=key, image_url=url)
        db.add(asset)
        try:
            await db.commit()
            preview = cos_service.submission_preview_url(key)
        except Exception as exc:
            # Commit before signing so a failed cleanup remains discoverable by the expiry job.
            await db.rollback()
            try:
                await cos_service.delete_submission_image(key)
            except Exception:
                logger.exception("Submission upload cleanup needed for %s", key)
            logger.warning("Submission upload unavailable: %s", type(exc).__name__)
            raise HTTPException(503, "图片上传暂时不可用，请稍后再试") from exc
        return SubmissionUploadOut(id=asset_id, image_url=preview)
    finally:
        await file.close()


@router.post("", response_model=SubmissionOut, status_code=201)
async def submit(
    body: SubmissionCreate,
    user: User = Depends(active_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
):
    await enforce_limit(redis, user.id, "create", 10, 3600)
    try:
        prompt = await submission_service.create_submission(db, user, body)
    except submission_service.SubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return await submission_service.to_out(db, prompt)


@router.get("", response_model=SubmissionList)
async def mine(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    user: User = Depends(active_user),
    db: AsyncSession = Depends(get_db),
):
    rows, total = await submission_service.list_submissions(db, user.id, None, page, page_size)
    return SubmissionList(
        items=[await submission_service.to_out(db, row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        has_more=page * page_size < total,
    )
