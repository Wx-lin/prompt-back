"""Read-only source control, guarded by the same administrator identity as reports."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.services import seo_sync_service
from app.utils.admin_auth import verify_admin_key

router = APIRouter(prefix="/sync/google", dependencies=[Depends(verify_admin_key)])


@router.get("/status")
async def status(response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return await seo_sync_service.sync_status(db)


@router.post("", status_code=202)
async def trigger(background_tasks: BackgroundTasks, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if not seo_sync_service.configured():
        raise HTTPException(409, "Google Search Console 尚未配置专用只读凭据")
    if seo_sync_service.get_settings().gsc_sync_mode == "external":
        raise HTTPException(409, "Google Search Console 由 GitHub Actions 同步，请通过工作流触发")
    attempt = await seo_sync_service.prepare_sync(AsyncSessionLocal)
    if attempt is None:
        raise HTTPException(409, "Google Search Console 同步正在进行中")
    background_tasks.add_task(seo_sync_service.run_configured_sync, prepared_at=attempt)
    return {"accepted": True}
