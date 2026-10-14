"""Admin-only submission review, separate from public routes."""

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.submission import SubmissionApprove, SubmissionList, SubmissionOut, SubmissionReject
from app.services import submission_service
from app.utils.admin_auth import verify_admin_key
from app.utils.category_catalog import CategoryKey

router = APIRouter(prefix="/api/admin/submissions", tags=["admin"], dependencies=[Depends(verify_admin_key)])
logger = logging.getLogger(__name__)


@router.get("", response_model=SubmissionList)
async def list_submissions(
    category: CategoryKey | None = None,
    status: Literal["pending", "approved", "rejected"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    rows, total = await submission_service.list_submissions(
        db, None, status, page, page_size, category=category
    )
    return SubmissionList(
        items=[await submission_service.to_out(db, row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
        has_more=page * page_size < total,
    )


async def review(db, prompt_id, approved, note=None, category=None):
    try:
        prompt = await submission_service.review_submission(db, prompt_id, approved, note, category=category)
    except submission_service.SubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.warning("Submission review unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "审核服务暂时不可用，请稍后再试") from exc
    if prompt is None:
        raise HTTPException(404, "作品不存在")
    return await submission_service.to_out(db, prompt)


@router.post("/{prompt_id}/approve", response_model=SubmissionOut)
async def approve(
    prompt_id: uuid.UUID, body: SubmissionApprove | None = None, db: AsyncSession = Depends(get_db)
):
    return await review(db, prompt_id, True, category=body.category if body else None)


@router.post("/{prompt_id}/reject", response_model=SubmissionOut)
async def reject(prompt_id: uuid.UUID, body: SubmissionReject, db: AsyncSession = Depends(get_db)):
    return await review(db, prompt_id, False, body.note)
