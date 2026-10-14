"""
爬虫采集草稿的业务逻辑：草稿列表查询、人工审核修正字段、转为正式内容、忽略草稿。

对应产品需求：docs/aigc-prompt-爬虫方案.md 的"脚本采集 → 人工审核 → 确认入库"流程。
草稿本身由离线爬虫脚本（scripts/crawler/）写入，这里只负责"审核之后"的操作。
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import CrawlDraft, Prompt, PromptImage
from app.schemas.crawl import CrawlDraftApprove, CrawlDraftReviewUpdate
from app.services import content_review_service, copyright_filter, cos_service
from app.utils.category_catalog import CATEGORY_LABELS
from app.utils.model_labels import normalize_model_labels

settings = get_settings()
logger = logging.getLogger(__name__)

# 和 crawl_task_service.PLATFORM_SPECS 的 cli_code 对应：MediaCrawler 落盘的媒体
# 目录按平台代号命名，local_path 形如 "{cli_code}/media/{content_id}/xxx.jpg"
_MEDIACRAWLER_PATH_PREFIXES = ("xhs/media/", "wb/media/", "ks/media/")


def _resolve_local_file_path(local_path: str) -> Path | None:
    """
    把 CrawlDraftImage.local_path 解析成本机绝对文件路径，供上传 COS 时使用。

    local_path 含义和 app/routers/admin.py 的 _resolve_image_url 保持一致，
    有三种历史格式（见该函数注释）：
    1. 完整远程 URL（早期小红书手写脚本直接存 CDN 地址）→ 已经是公网地址，
       不需要上传，返回 None 让调用方直接复用原值
    2. MediaCrawler 的 data/ 目录相对路径 → 拼接 MEDIACRAWLER_DIR/data/
    3. crawl_media_dir 下的本地相对路径（早期手写脚本自己下载的图片）
       → 拼接 crawl_media_dir
    """
    if local_path.startswith("http"):
        return None
    if local_path.startswith(_MEDIACRAWLER_PATH_PREFIXES):
        mediacrawler_dir = Path(settings.mediacrawler_dir).expanduser().resolve()
        return mediacrawler_dir / "data" / local_path
    return Path(settings.crawl_media_dir).expanduser().resolve() / local_path


async def list_drafts(
    db: AsyncSession,
    status_filter: str | None,
    page: int,
    page_size: int,
    category: str | None = None,
) -> tuple[list[CrawlDraft], int]:
    """
    草稿列表查询，默认按创建时间倒序（最新采集的排前面）。
    支持按 status 筛选（pending / approved / rejected），对应后台"只看待审核"的常见操作。
    """
    query = select(CrawlDraft)
    if category is not None:
        query = query.where(CrawlDraft.extracted_category == category)
    if status_filter:
        query = query.where(CrawlDraft.status == status_filter)

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    list_query = (
        query.options(selectinload(CrawlDraft.images))
        .order_by(CrawlDraft.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(list_query)).scalars().all()
    return list(rows), total


async def get_draft(db: AsyncSession, draft_id: uuid.UUID) -> CrawlDraft | None:
    """查看单条草稿详情，审核前通常先调用这个看完整原始素材。"""
    query = select(CrawlDraft).where(CrawlDraft.id == draft_id).options(selectinload(CrawlDraft.images))
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def update_draft_review_fields(
    db: AsyncSession, draft_id: uuid.UUID, body: CrawlDraftReviewUpdate
) -> CrawlDraft | None:
    """
    人工审核时修正草稿的"干净字段"（提示词原文、分类、模型标签），不改变 status。

    这一步可以反复调用（审核员可能需要多次修改才能定稿），只有调用"审核通过"接口
    才会真正生成正式内容。
    """
    draft = await get_draft(db, draft_id)
    if draft is None:
        return None

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        if field == "extracted_models" and value is not None:
            value = ",".join(normalize_model_labels(value.split(",")))
        setattr(draft, field, value)

    await db.commit()
    await db.refresh(draft)
    await db.refresh(draft, attribute_names=["images"])
    return draft


async def approve_draft(db: AsyncSession, draft_id: uuid.UUID, body: CrawlDraftApprove) -> Prompt | None:
    """
    审核通过，把草稿转为正式的 Prompt 内容。

    字段优先级：请求体里显式传的值 > 草稿上已有的 extracted_* 字段。
    转化成功后草稿状态变为 approved，记录 approved_prompt_id，防止同一条草稿被重复转化。

    草稿配图处理（2026-10-09 接入腾讯云 COS 后的正式方案）：
    - 本地文件（MediaCrawler/早期手写脚本下载到本机的图片）会先上传到 COS，
      Prompt 的 image_url 存 COS 返回的公网 URL，不再直接复用本地相对路径——
      发布后的内容不应该依赖"这台 Mac 一直开机运行后端服务"这个前提。
    - 如果某条草稿图片的 local_path 本身已经是完整 URL（早期个别脚本直接存了
      CDN 地址），跳过上传，原样复用，避免重复搬运。
    - 单张图片上传失败不应该让整条审核通过操作全部失败（可能是某张图片文件
      已被清理等个别情况），改为跳过这张图、在返回前抛出汇总错误提醒运营方，
      但不阻塞其他已成功上传的图片入库——取舍参考：宁可内容先带着少一张图
      发出去、运营方事后手动补，也不要让一张坏图卡住整条内容审核流程。
    """
    draft = await get_draft(db, draft_id)
    if draft is None:
        return None
    if draft.status == "approved":
        raise ValueError("该草稿已经转为正式内容，不能重复操作")

    # 版权声明硬拦截：原作者在正文里明确写了"禁止转载/禁止商用"等声明的，直接拒绝
    # 转为正式内容，不进入 AI 预审（AI 判断准确率不足以兜底版权风险，必须用确定性
    # 规则先挡一道），详见 app/services/copyright_filter.py 顶部说明。
    hit_keyword = copyright_filter.contains_copyright_restriction(
        draft.raw_content or ""
    ) or copyright_filter.contains_copyright_restriction(
        copyright_filter.strip_emoji_and_punctuation(draft.raw_content or "")
    )
    if hit_keyword:
        raise ValueError(
            f"原文包含版权限制声明（命中关键词：{hit_keyword}），不能转为正式内容，请联系原作者授权或放弃该草稿"
        )

    prompt_text = body.prompt_text or draft.extracted_prompt_text
    category = body.category or draft.extracted_category
    if not prompt_text or category not in CATEGORY_LABELS:
        raise ValueError("提示词原文和分类必须有值（可以在请求体里传，也可以提前在草稿里填好）")

    models_source = body.models
    if models_source is None:
        raw_models = draft.extracted_models or ""
        models_source = [m.strip() for m in raw_models.split(",") if m.strip()]

    # AI 内容预审：在 COS 上传和入库之前，用 qwen3.7-plus 对图片和文案做一次自动评估。
    # 只传已经是公网 URL 的图片（local_path 以 http 开头的），本地磁盘路径无法被
    # 远端 AI 服务访问，跳过不传——文案清洗和分类建议仍然会执行。
    # 审核结果是"建议"而非"拦截"：调用失败时降级处理，不阻塞审核流程。
    # 如果请求体里已经显式提供了 prompt_text / category，优先用请求体的值；
    # 没有提供时才用 AI 建议的值（避免运营方手动填好了又被 AI 覆盖）。
    public_image_urls = [img.local_path for img in draft.images if img.local_path.startswith("http")]
    try:
        review = await content_review_service.review_draft_content(
            raw_text=draft.raw_content or "",
            image_urls=sorted(
                public_image_urls,
                key=lambda u: next((img.sort_order for img in draft.images if img.local_path == u), 0),
            ),
        )
        logger.info(
            "草稿 %s AI 预审完成：%d 张图可信=%s，建议分类=%s，建议标题=%r",
            draft_id,
            len(review.images),
            [img.is_credible for img in review.images],
            review.suggested_category,
            review.suggested_title,
        )
        if not review.has_credible_image and public_image_urls:
            logger.warning(
                "草稿 %s 的所有图片均被 AI 判断为不可信（非 AI 效果图），请人工确认后再决定是否通过。",
                draft_id,
            )
        # 用 AI 建议补全未显式传入的字段
        if not body.prompt_text and not draft.extracted_prompt_text and review.cleaned_prompt_text:
            prompt_text = review.cleaned_prompt_text
        if not body.category and not draft.extracted_category and review.suggested_category:
            category = review.suggested_category
    except content_review_service.ContentReviewError as exc:
        logger.warning("草稿 %s AI 预审失败（已降级，不阻塞审核）：%s", draft_id, exc)

    # 先把所有需要上传的图片处理好，再写数据库——避免上传过程中途失败时
    # 留下一个"部分字段已写入但图片没传完"的半成品 Prompt。
    resolved_images: list[tuple[str, int]] = []  # (image_url, sort_order)
    upload_errors: list[str] = []
    for image in draft.images:
        local_file = _resolve_local_file_path(image.local_path)
        if local_file is None:
            # local_path 本身已经是完整 URL，直接复用
            resolved_images.append((image.local_path, image.sort_order))
            continue
        try:
            image_url = await cos_service.upload_prompt_image(local_file)
            resolved_images.append((image_url, image.sort_order))
        except cos_service.CosUploadError as exc:
            upload_errors.append(f"第 {image.sort_order + 1} 张图片上传失败：{exc}")

    if upload_errors:
        logger.warning(
            "草稿 %s 审核通过时部分图片上传 COS 失败（已跳过，不阻塞本次审核）：%s",
            draft_id,
            "; ".join(upload_errors),
        )

    author_avatar_url = None
    if draft.author_avatar_url:
        try:
            author_avatar_url = await cos_service.upload_author_avatar(draft.author_avatar_url)
        except cos_service.CosUploadError as exc:
            logger.warning("草稿 %s 作者头像转存失败：%s", draft_id, exc)

    prompt = Prompt(
        title=body.title,
        prompt_text=prompt_text,
        category=category,
        models=normalize_model_labels(models_source),
        author_name=draft.author_name,
        author_avatar_url=author_avatar_url,
        source_url=draft.source_url,
        is_published=body.is_published,
    )
    db.add(prompt)
    await db.flush()

    for image_url, sort_order in resolved_images:
        db.add(
            PromptImage(
                prompt_id=prompt.id,
                image_url=image_url,
                # 目前没有接入图片处理服务生成真正的缩略图，thumbnail_url 直接
                # 复用原图地址——首页列表接口（prompt_service.list_prompts）读的
                # 是 thumbnail_url，留空会导致列表页卡片没有封面图（2026-10-09
                # 真实验证中发现：4条新审核内容首页全部空白，根因就是这里漏填）。
                thumbnail_url=image_url,
                sort_order=sort_order,
            )
        )

    draft.status = "approved"
    draft.reviewed_at = datetime.now()
    draft.approved_prompt_id = prompt.id

    await db.commit()
    await db.refresh(prompt)
    await db.refresh(prompt, attribute_names=["images"])
    return prompt


async def reject_draft(db: AsyncSession, draft_id: uuid.UUID, note: str | None) -> CrawlDraft | None:
    """忽略一条草稿（不转为正式内容），用于筛掉抓到的低质量/不相关素材。"""
    draft = await get_draft(db, draft_id)
    if draft is None:
        return None

    draft.status = "rejected"
    draft.reviewed_at = datetime.now()
    draft.review_note = note

    await db.commit()
    await db.refresh(draft)
    return draft
