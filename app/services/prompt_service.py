"""
提示词相关业务逻辑：列表查询、详情查询、点赞/收藏、相似推荐。

路由层只负责解析请求参数和拼装响应，具体的数据库查询逻辑都放在这一层，
方便单独编写单元测试，也方便未来替换缓存策略时不影响路由代码。
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import CategoryLegacyMembership, Prompt, PromptImage, UserCollect, UserLike
from app.schemas.prompt import PromptListItem, SortOption
from app.services.homepage_service import thumbnail_for_prompt
from app.services.model_service import matching_stored_labels
from app.utils.category_catalog import CATEGORY_LABELS, LEGACY_CATEGORY_KEYS
from app.utils.public_prompts import public_prompt_condition


async def list_prompts(
    db: AsyncSession,
    category: str | None,
    models: list[str] | None,
    sort: SortOption,
    page: int,
    page_size: int,
    search: str | None = None,
    require_image: bool = False,
) -> tuple[list[PromptListItem], int]:
    """
    首页/分类页/模型筛选的分页列表查询，对应 PRD 3.1、3.3。

    返回 (当前页数据, 总条数)，总条数用于前端计算是否还有下一页。
    """
    base_query = select(Prompt).where(public_prompt_condition())

    if require_image:
        base_query = base_query.where(Prompt.images.any(func.trim(PromptImage.image_url) != ""))

    if category is not None and category != "all":
        if category in LEGACY_CATEGORY_KEYS:
            base_query = base_query.where(
                Prompt.id.in_(
                    select(CategoryLegacyMembership.prompt_id).where(
                        CategoryLegacyMembership.legacy_key == category
                    )
                )
            )
        elif category in CATEGORY_LABELS:
            base_query = base_query.where(Prompt.category == category)
        else:
            raise HTTPException(status_code=422, detail="请选择有效分类")
    if models:
        aliases = await matching_stored_labels(db, models)
        base_query = base_query.where(Prompt.models.op("&&")(array(aliases)))
    if search is not None:
        base_query = base_query.where(
            or_(
                Prompt.title.ilike(f"%{search}%"),
                Prompt.description.ilike(f"%{search}%"),
            )
        )

    sort_columns = (
        [
            case(
                (Prompt.images.any(func.trim(PromptImage.image_url) != ""), Prompt.homepage_rank),
                else_=None,
            )
            .asc()
            .nulls_last(),
            Prompt.like_count.desc(),
            Prompt.created_at.desc(),
            Prompt.id.desc(),
        ]
        if sort == SortOption.RECOMMENDED
        else [
            {
                SortOption.LATEST: Prompt.created_at.desc(),
                SortOption.POPULAR: Prompt.like_count.desc(),
                SortOption.HOT: Prompt.view_count.desc(),
            }[sort],
            Prompt.id.desc(),
        ]
    )

    count_query = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    # 用 selectinload 预加载第一张效果图，避免 N+1 查询问题
    list_query = (
        base_query.options(selectinload(Prompt.images))
        .order_by(*sort_columns)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(list_query)).scalars().all()

    items = [
        PromptListItem(
            id=row.id,
            title=row.title,
            category=row.category,
            models=row.models,
            thumbnail_url=thumbnail_for_prompt(row),
            like_count=row.like_count,
            view_count=row.view_count,
            author_name=row.author_name,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return items, total


async def get_prompt_detail(db: AsyncSession, prompt_id: uuid.UUID) -> Prompt | None:
    """获取单条提示词详情（含关联的效果图），对应 PRD 3.2 详情弹窗。"""
    query = (
        select(Prompt)
        .where(Prompt.id == prompt_id, public_prompt_condition())
        .options(selectinload(Prompt.images))
    )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def get_similar_prompts(
    db: AsyncSession, prompt_id: uuid.UUID, category: str, limit: int = 6
) -> list[PromptListItem]:
    """获取同分类下的相似内容，排除自身，用于详情弹窗"更多相似"区域。"""
    query = (
        select(Prompt)
        .where(Prompt.category == category, Prompt.id != prompt_id, public_prompt_condition())
        .options(selectinload(Prompt.images))
        .order_by(Prompt.like_count.desc())
        .limit(limit)
    )
    rows = (await db.execute(query)).scalars().all()
    return [
        PromptListItem(
            id=row.id,
            title=row.title,
            category=row.category,
            models=row.models,
            thumbnail_url=thumbnail_for_prompt(row),
            like_count=row.like_count,
            view_count=row.view_count,
            author_name=row.author_name,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def is_liked_by_user(db: AsyncSession, user_id: uuid.UUID | None, prompt_id: uuid.UUID) -> bool:
    """判断当前用户是否已点赞该内容，未登录用户固定返回 False。"""
    if user_id is None:
        return False
    query = select(UserLike).where(UserLike.user_id == user_id, UserLike.prompt_id == prompt_id)
    return (await db.execute(query)).scalar_one_or_none() is not None


async def is_collected_by_user(db: AsyncSession, user_id: uuid.UUID | None, prompt_id: uuid.UUID) -> bool:
    """判断当前用户是否已收藏该内容，未登录用户固定返回 False。"""
    if user_id is None:
        return False
    query = select(UserCollect).where(UserCollect.user_id == user_id, UserCollect.prompt_id == prompt_id)
    return (await db.execute(query)).scalar_one_or_none() is not None


async def toggle_like(db: AsyncSession, user_id: uuid.UUID, prompt_id: uuid.UUID, like: bool) -> None:
    """
    点赞/取消点赞，并同步维护 Prompt.like_count 冗余计数字段。

    冗余计数是为了首页列表排序（按热度）时不用每次都 COUNT 关联表，
    用空间换查询速度，这是瀑布流这类高频读场景下常见的权衡。
    """
    existing = await db.execute(
        select(UserLike).where(UserLike.user_id == user_id, UserLike.prompt_id == prompt_id)
    )
    row = existing.scalar_one_or_none()

    prompt = await db.get(Prompt, prompt_id)
    if prompt is None:
        return

    if like and row is None:
        db.add(UserLike(user_id=user_id, prompt_id=prompt_id))
        prompt.like_count += 1
    elif not like and row is not None:
        await db.delete(row)
        prompt.like_count = max(0, prompt.like_count - 1)

    await db.commit()


async def toggle_collect(db: AsyncSession, user_id: uuid.UUID, prompt_id: uuid.UUID, collect: bool) -> None:
    """收藏/取消收藏，逻辑与 toggle_like 对称，同步维护 Prompt.collect_count。"""
    existing = await db.execute(
        select(UserCollect).where(UserCollect.user_id == user_id, UserCollect.prompt_id == prompt_id)
    )
    row = existing.scalar_one_or_none()

    prompt = await db.get(Prompt, prompt_id)
    if prompt is None:
        return

    if collect and row is None:
        db.add(UserCollect(user_id=user_id, prompt_id=prompt_id))
        prompt.collect_count += 1
    elif not collect and row is not None:
        await db.delete(row)
        prompt.collect_count = max(0, prompt.collect_count - 1)

    await db.commit()
