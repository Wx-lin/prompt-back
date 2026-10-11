"""Authenticated schedule settings, execution history and login assistance."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CrawlSchedule, CrawlTask
from app.schemas.crawl_schedule import CrawlScheduleCreate, CrawlScheduleOut, CrawlScheduleUpdate
from app.schemas.crawl_task import CrawlTaskOut
from app.services import crawl_schedule_service as service
from app.utils.admin_auth import verify_admin_key

router = APIRouter(prefix="/api/admin", tags=["crawl-operations"], dependencies=[Depends(verify_admin_key)])


async def require_schedule(schedule_id, db, lock=False):
    query = select(CrawlSchedule).where(CrawlSchedule.id == schedule_id)
    if lock:
        query = query.with_for_update()
    schedule = await db.scalar(query)
    if schedule is None:
        raise HTTPException(404, "定时任务不存在")
    return schedule


@router.get("/crawl-schedules", response_model=list[CrawlScheduleOut])
async def schedules(db: AsyncSession = Depends(get_db)):
    return await service.list_schedules(db)


@router.post("/crawl-schedules", response_model=CrawlScheduleOut, status_code=201)
async def create(payload: CrawlScheduleCreate, db: AsyncSession = Depends(get_db)):
    return await service.save_schedule(db, payload)


@router.patch("/crawl-schedules/{schedule_id}", response_model=CrawlScheduleOut)
async def update(schedule_id: uuid.UUID, payload: CrawlScheduleUpdate, db: AsyncSession = Depends(get_db)):
    schedule = await require_schedule(schedule_id, db, lock=True)
    try:
        return await service.save_schedule(db, payload, schedule)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/crawl-schedules/{schedule_id}/run", response_model=list[CrawlTaskOut])
async def run(schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await require_schedule(schedule_id, db)
    from scripts.crawl_dispatch import enqueue_schedule

    return await enqueue_schedule(schedule_id)


@router.get("/crawl-runs")
async def runs(
    date: date | None = None,
    status: str | None = None,
    schedule_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if date is not None:
        start = datetime.combine(date, time.min, tzinfo=service.SHANGHAI).astimezone(UTC)
        filters.extend([CrawlTask.created_at >= start, CrawlTask.created_at < start + timedelta(days=1)])
    if status:
        if status not in {"pending", "running", "awaiting_login", "reviewing", "success", "failed"}:
            raise HTTPException(422, "无效状态")
        filters.append(CrawlTask.status == status)
    if schedule_id:
        filters.append(CrawlTask.schedule_id == schedule_id)
    total = await db.scalar(select(func.count()).select_from(CrawlTask).where(*filters))
    tasks = list(
        await db.scalars(
            select(CrawlTask)
            .where(*filters)
            .order_by(CrawlTask.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    grouped = (
        await db.execute(select(CrawlTask.status, func.count()).where(*filters).group_by(CrawlTask.status))
    ).all()
    counts = dict(grouped)
    sums = (
        await db.execute(
            select(
                func.coalesce(func.sum(CrawlTask.new_draft_count), 0),
                func.coalesce(func.sum(CrawlTask.published_count), 0),
                func.coalesce(func.sum(CrawlTask.held_count), 0),
            ).where(*filters)
        )
    ).one()
    summary = dict(
        total=total,
        success=counts.get("success", 0),
        failed=counts.get("failed", 0),
        running=sum(counts.get(s, 0) for s in ["pending", "running", "awaiting_login", "reviewing"]),
        new_drafts=sums[0],
        published=sums[1],
        held=sums[2],
    )
    return dict(
        items=[CrawlTaskOut.model_validate(t) for t in tasks],
        total=total,
        page=page,
        page_size=page_size,
        summary=summary,
    )


async def require_task(task_id, db):
    task = await db.get(CrawlTask, task_id)
    if task is None:
        raise HTTPException(404, "采集任务不存在")
    return task


def valid_qr_metadata(metadata):
    if metadata.get("state") != "awaiting_login" or not metadata.get("qr_version"):
        return False
    try:
        expires_at = datetime.fromisoformat(metadata["expires_at"])
        return expires_at.tzinfo is not None and expires_at > datetime.now(UTC)
    except (KeyError, TypeError, ValueError):
        return False


@router.get("/crawl-tasks/{task_id}/login")
async def login(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await require_task(task_id, db)
    from app.services.crawl_login_service import get_login_metadata

    if task.status != "awaiting_login":
        return dict(
            required=False,
            status=task.status,
            qr_available=False,
            expires_at=None,
            qr_version=None,
            message=task.error_message or "",
        )
    metadata = get_login_metadata(task_id)
    from app.services.crawl_login_service import login_image_path

    return dict(
        required=True,
        status=metadata.get("state", "awaiting_login"),
        qr_available=valid_qr_metadata(metadata) and login_image_path(task_id).is_file(),
        expires_at=metadata.get("expires_at"),
        qr_version=metadata.get("qr_version"),
        message=metadata.get("message", "等待二维码"),
    )


@router.get("/crawl-tasks/{task_id}/login-qr")
async def login_qr(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await require_task(task_id, db)
    from app.services.crawl_login_service import get_login_metadata, login_image_path

    path = login_image_path(task_id)
    metadata = get_login_metadata(task_id)
    if task.status != "awaiting_login" or not valid_qr_metadata(metadata) or not path.is_file():
        raise HTTPException(404, "二维码未准备好或任务已结束")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/crawl-tasks/{task_id}/login/refresh")
async def refresh(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await require_task(task_id, db)
    if task.status != "awaiting_login":
        raise HTTPException(409, "当前任务无需登录")
    from app.services.crawl_login_service import request_login_refresh

    try:
        request_login_refresh(task_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"message": "二维码刷新已请求"}
