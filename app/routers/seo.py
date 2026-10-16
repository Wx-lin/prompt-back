"""Administrator-only search performance; external accounts are never implied connected."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.seo import Engine, SeoImport
from app.services.seo_service import get_report, import_snapshot
from app.utils.admin_auth import verify_admin_key

router = APIRouter(prefix="/api/admin/seo", tags=["SEO"], dependencies=[Depends(verify_admin_key)])


@router.get("")
async def report(
    response: Response,
    engine: Engine = "google",
    start_date: date | None = None,
    end_date: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    zone = "America/Los_Angeles" if engine == "google" else "Asia/Shanghai"
    end = end_date or datetime.now(ZoneInfo(zone)).date() - timedelta(days=1)
    start = start_date or end - timedelta(days=27)
    if start > end or (end - start).days > 365:
        raise HTTPException(422, "日期范围必须有序且不超过 366 天")
    return await get_report(db, engine, start, end)


@router.post("/import")
async def import_report(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2_000_000:
            raise HTTPException(413, "导入文件不得超过 2 MB")
        body.extend(chunk)
    try:
        payload = SeoImport.model_validate_json(bytes(body))
    except ValidationError:
        raise HTTPException(422, "导入数据格式不正确，请检查维度、日期、地址及指标") from None
    return await import_snapshot(db, payload)
