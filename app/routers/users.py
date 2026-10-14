"""
用户相关路由：我的收藏列表。

对应 PRD 3.6"我的收藏页"、后端技术选型.md 接口清单 GET /api/users/me/collects。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Prompt, User, UserCollect
from app.schemas.prompt import PromptListItem, PromptListResponse
from app.utils.deps import get_current_user
from app.utils.public_prompts import public_prompt_condition

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/me/collects", response_model=PromptListResponse)
async def get_my_collects(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PromptListResponse:
    """获取当前用户收藏的所有内容，瀑布流样式，按收藏时间倒序。"""
    base_query = (
        select(Prompt)
        .join(UserCollect, UserCollect.prompt_id == Prompt.id)
        .where(UserCollect.user_id == current_user.id, public_prompt_condition())
        .options(selectinload(Prompt.images))
        .order_by(UserCollect.created_at.desc())
    )

    total = (await db.execute(select(func.count()).select_from(base_query.subquery()))).scalar_one()
    rows = (await db.execute(base_query.offset((page - 1) * page_size).limit(page_size))).scalars().all()

    items = [
        PromptListItem(
            id=row.id,
            title=row.title,
            category=row.category,
            models=row.models,
            thumbnail_url=row.images[0].thumbnail_url if row.images else None,
            like_count=row.like_count,
            view_count=row.view_count,
            author_name=row.author_name,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return PromptListResponse(
        items=items, total=total, page=page, page_size=page_size, has_more=page * page_size < total
    )
