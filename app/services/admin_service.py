"""
后台管理业务逻辑：内容的增删改查、举报的查看与处理。

对应产品需求：PRD 3.5 内容管理（后台运营，非用户可见）。路由层只负责参数解析
和权限校验（见 app/utils/admin_auth.py），具体的数据库操作都放在这一层。
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func as sql_func

from app.models import ContentReport, Prompt, PromptImage
from app.schemas.admin import AdminPromptCreate, AdminPromptUpdate


async def list_prompts_for_admin(
    db: AsyncSession,
    is_published: bool | None,
    page: int,
    page_size: int,
) -> tuple[list[Prompt], int]:
    """
    后台内容列表查询，和对外接口 prompt_service.list_prompts 的区别：
    - 不过滤 is_published（默认能看到全部内容，包括草稿态）
    - 支持按 is_published 显式筛选（给"只看待审核草稿"这种场景用）
    - 按创建时间倒序，方便看到最新采集的内容
    """
    query = select(Prompt)
    if is_published is not None:
        query = query.where(Prompt.is_published.is_(is_published))

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    list_query = (
        query.options(selectinload(Prompt.images))
        .order_by(Prompt.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(list_query)).scalars().all()
    return list(rows), total


async def get_prompt_for_admin(db: AsyncSession, prompt_id: uuid.UUID) -> Prompt | None:
    """后台查看单条内容详情（含草稿态），用于编辑前先拿到当前完整数据。"""
    query = select(Prompt).where(Prompt.id == prompt_id).options(selectinload(Prompt.images))
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def create_prompt(db: AsyncSession, body: AdminPromptCreate) -> Prompt:
    """
    创建一条新内容，对应采集流程"人工审核确认后入库"这一步。

    images 列表在同一个事务里一起插入，保证内容和图片不会出现"写了一半"的脏数据。
    """
    prompt = Prompt(
        title=body.title,
        prompt_text=body.prompt_text,
        description=body.description,
        category=body.category,
        models=body.models,
        author_name=body.author_name,
        source_url=body.source_url,
        is_published=body.is_published,
    )
    db.add(prompt)
    await db.flush()  # 先拿到 prompt.id，供下面的效果图外键引用

    for image_in in body.images:
        db.add(
            PromptImage(
                prompt_id=prompt.id,
                image_url=image_in.image_url,
                thumbnail_url=image_in.thumbnail_url,
                sort_order=image_in.sort_order,
            )
        )

    await db.commit()
    # 整体 refresh（不只刷新 images），避免 updated_at 等字段序列化时触发异步懒加载冲突。
    await db.refresh(prompt)
    await db.refresh(prompt, attribute_names=["images"])
    return prompt


async def update_prompt(db: AsyncSession, prompt_id: uuid.UUID, body: AdminPromptUpdate) -> Prompt | None:
    """
    编辑一条内容，支持部分字段更新（PATCH 语义）。

    images 字段如果传了（不是 None），会先删除原有的全部效果图再整体插入新的，
    这是 MVP 阶段的简化处理，不支持"只改第二张图的排序"这种细粒度操作。
    """
    prompt = await get_prompt_for_admin(db, prompt_id)
    if prompt is None:
        return None

    update_data = body.model_dump(exclude_unset=True, exclude={"images"})
    for field, value in update_data.items():
        setattr(prompt, field, value)

    if body.images is not None:
        prompt.images.clear()  # cascade="all, delete-orphan" 会处理旧图片的删除
        for image_in in body.images:
            prompt.images.append(
                PromptImage(
                    image_url=image_in.image_url,
                    thumbnail_url=image_in.thumbnail_url,
                    sort_order=image_in.sort_order,
                )
            )

    await db.commit()
    # 不能只 refresh "images" 一个字段：updated_at 由数据库 onupdate 触发器更新，
    # 本地对象上还是旧值，如果序列化时才去访问会触发异步懒加载冲突（MissingGreenlet）。
    # 真实测试中发现这个 bug：PATCH 接口第一次调用直接 500，详见 PROGRESS.md。
    await db.refresh(prompt)
    await db.refresh(prompt, attribute_names=["images"])
    return prompt


async def delete_prompt(db: AsyncSession, prompt_id: uuid.UUID) -> bool:
    """
    彻底删除一条内容（级联删除关联的效果图和举报记录）。

    MVP 阶段没有"软删除/回收站"机制，删除即不可恢复，调用方需要自行确认。
    """
    prompt = await db.get(Prompt, prompt_id)
    if prompt is None:
        return False
    await db.delete(prompt)
    await db.commit()
    return True


async def set_published(db: AsyncSession, prompt_id: uuid.UUID, is_published: bool) -> Prompt | None:
    """单独的发布/下架操作，比完整的 update_prompt 更轻量，常用于"审核通过一键发布"场景。"""
    prompt = await db.get(Prompt, prompt_id)
    if prompt is None:
        return None
    prompt.is_published = is_published
    await db.commit()
    await db.refresh(prompt)
    return prompt


async def list_reports(
    db: AsyncSession,
    status_filter: str | None,
    page: int,
    page_size: int,
) -> tuple[list[tuple[ContentReport, str | None]], int]:
    """
    举报列表查询，附带关联的 Prompt.title（用 LEFT JOIN，避免对应内容已被删除时查询报错）。

    返回 (ContentReport, prompt_title) 的元组列表，prompt_title 可能为 None
    （对应内容已被删除的边界情况）。
    """
    query = select(ContentReport, Prompt.title).outerjoin(Prompt, ContentReport.prompt_id == Prompt.id)
    if status_filter:
        query = query.where(ContentReport.status == status_filter)

    count_query = select(sql_func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    list_query = (
        query.order_by(ContentReport.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    rows = (await db.execute(list_query)).all()
    return [(row[0], row[1]) for row in rows], total


async def handle_report(
    db: AsyncSession,
    report_id: uuid.UUID,
    handled_note: str | None,
    unpublish_prompt: bool,
) -> ContentReport | None:
    """
    处理一条举报：标记为 resolved，记录处理备注和处理时间；
    如果 unpublish_prompt=True，联动把对应内容下架（is_published=False）。
    """
    report = await db.get(ContentReport, report_id)
    if report is None:
        return None

    report.status = "resolved"
    report.handled_note = handled_note
    report.handled_at = datetime.now()

    if unpublish_prompt:
        prompt = await db.get(Prompt, report.prompt_id)
        if prompt is not None:
            prompt.is_published = False

    await db.commit()
    await db.refresh(report)
    return report
