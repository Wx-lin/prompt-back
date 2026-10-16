"""Administrator-only, read-only dashboard and user directory."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.admin_insights import AdminDashboard, AdminUserList
from app.services import admin_insights_service
from app.utils.admin_auth import verify_admin_key

router = APIRouter(prefix="/api/admin", tags=["后台数据"], dependencies=[Depends(verify_admin_key)])


@router.get("/dashboard", response_model=AdminDashboard)
async def dashboard(response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return await admin_insights_service.get_dashboard(db)


@router.get("/users", response_model=AdminUserList)
async def users(
    response: Response,
    search: str = Query("", max_length=200),
    is_active: Literal["true", "false"] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    active = None if is_active is None else is_active == "true"
    return await admin_insights_service.list_users(db, search, active, page, page_size)
