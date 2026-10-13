"""
提示词相关路由：首页列表、详情、相似推荐、搜索、浏览/点赞/收藏、举报。

对应 PRD 3.1~3.4，3.5，以及后端技术选型.md 3.1~3.3 节的接口清单。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ContentReport, User
from app.redis import get_redis_client
from app.schemas.prompt import (
    PromptDetailOut,
    PromptListResponse,
    ReportCreate,
    SortOption,
)
from app.services import prompt_service, view_count_service
from app.utils.deps import get_current_user, get_optional_current_user

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("", response_model=PromptListResponse)
async def list_prompts(
    category: str | None = Query(default=None, description="分类过滤，不传则全部"),
    models: list[str] | None = Query(default=None, description="模型标签过滤，多选 OR 逻辑"),
    sort: SortOption = Query(default=SortOption.LATEST),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> PromptListResponse:
    """首页瀑布流 / 分类筛选 / 模型筛选的统一分页查询入口，对应 PRD 3.1、3.3。"""
    items, total = await prompt_service.list_prompts(db, category, models, sort, page, page_size)
    return PromptListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=page * page_size < total,
    )


@router.get("/search", response_model=PromptListResponse)
async def search_prompts(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    category: str | None = Query(default=None),
    models: list[str] | None = Query(default=None),
    sort: SortOption = Query(default=SortOption.LATEST),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> PromptListResponse:
    """Search, filters and ordering share the feed query before pagination."""
    items, total = await prompt_service.list_prompts(db, category, models, sort, page, page_size, search=q)
    return PromptListResponse(
        items=items, total=total, page=page, page_size=page_size, has_more=page * page_size < total
    )


@router.get("/{prompt_id}", response_model=PromptDetailOut)
async def get_prompt_detail(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
) -> PromptDetailOut:
    """详情弹窗数据，对应 PRD 3.2。匿名可访问，登录用户会额外返回是否已点赞/收藏。"""
    prompt = await prompt_service.get_prompt_detail(db, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="提示词不存在")

    user_id = current_user.id if current_user else None
    is_liked = await prompt_service.is_liked_by_user(db, user_id, prompt_id)
    is_collected = await prompt_service.is_collected_by_user(db, user_id, prompt_id)

    detail = PromptDetailOut.model_validate(prompt)
    detail.is_liked = is_liked
    detail.is_collected = is_collected
    return detail


@router.get("/{prompt_id}/similar", response_model=list)
async def get_similar(prompt_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """详情弹窗"更多相似"区域数据，对应 PRD 3.2。"""
    prompt = await prompt_service.get_prompt_detail(db, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="提示词不存在")
    return await prompt_service.get_similar_prompts(db, prompt_id, prompt.category)


@router.post("/{prompt_id}/view", status_code=204)
async def record_view(
    prompt_id: uuid.UUID,
    redis_client: Redis = Depends(get_redis_client),
) -> None:
    """记录一次浏览，匿名也可调用，用于浏览量统计（对应 PRD 排序"按浏览量"）。"""
    await view_count_service.record_view(redis_client, prompt_id)


@router.post("/{prompt_id}/like", status_code=204)
async def like_prompt(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """点赞，需登录。对应 PRD 3.6。"""
    await prompt_service.toggle_like(db, current_user.id, prompt_id, like=True)


@router.delete("/{prompt_id}/like", status_code=204)
async def unlike_prompt(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """取消点赞，需登录。"""
    await prompt_service.toggle_like(db, current_user.id, prompt_id, like=False)


@router.post("/{prompt_id}/collect", status_code=204)
async def collect_prompt(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """收藏，需登录。对应 PRD 3.6。"""
    await prompt_service.toggle_collect(db, current_user.id, prompt_id, collect=True)


@router.delete("/{prompt_id}/collect", status_code=204)
async def uncollect_prompt(
    prompt_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """取消收藏，需登录。"""
    await prompt_service.toggle_collect(db, current_user.id, prompt_id, collect=False)


@router.post("/{prompt_id}/report", status_code=204)
async def report_prompt(prompt_id: uuid.UUID, body: ReportCreate, db: AsyncSession = Depends(get_db)) -> None:
    """
    举报/下架申诉，对应 PRD 3.2"内容有误或需要下架？联系我们"（P0）。

    不要求登录，任何人（包括原作者本人）都可以直接提交申诉，运营方人工处理
    （见 PRD 3.5"下架申诉处理"：24-48 小时内处理）。
    """
    db.add(ContentReport(prompt_id=prompt_id, reporter_email=body.reporter_email, reason=body.reason))
    await db.commit()
