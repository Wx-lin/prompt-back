"""Fail-closed publication for explicitly selected newly crawled drafts."""

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import CrawlDraft, Prompt, PromptImage
from app.services import content_review_service, cos_service
from app.services.crawl_service import _resolve_local_file_path
from app.utils.category_catalog import CATEGORY_LABELS
from app.utils.model_labels import normalize_model_labels

logger = logging.getLogger(__name__)


async def _upload_images(draft):
    urls = []
    for image in sorted(draft.images, key=lambda item: item.sort_order):
        local = _resolve_local_file_path(image.local_path)
        if local is None:
            url = await cos_service.migrate_remote_image_to_cos(image.local_path)
        else:
            url = await cos_service.upload_prompt_image(local)
        urls.append(url)
    return urls


async def auto_publish_draft(db, draft_id) -> str:
    """Lock a draft; failures stay pending for human review, never publish by default."""
    result = await db.execute(
        select(CrawlDraft)
        .where(CrawlDraft.id == draft_id)
        .options(selectinload(CrawlDraft.images))
        .with_for_update()
    )
    draft = result.scalar_one_or_none()
    if draft is None or draft.status != "pending" or draft.approved_prompt_id:
        return "skipped"
    review = None
    try:
        urls = await _upload_images(draft)
        if not urls:
            raise ValueError("没有可审核图片")
        review = await content_review_service.review_draft_content(
            raw_text=draft.raw_content or "",
            image_urls=urls,
        )
        if not review.cleaned_prompt_text.strip() or not review.suggested_title.strip():
            raise ValueError("没有有效提示词或标题")
        if review.suggested_category not in CATEGORY_LABELS:
            raise ValueError("分类用途不明确，需人工确认")
        credible_urls = [urls[item.index] for item in review.images if item.is_credible is True]
        if not credible_urls:
            raise ValueError("没有通过审核的 AI 图片")
    except Exception as exc:
        logger.warning("Auto review held draft %s (%s)", draft_id, type(exc).__name__)
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        draft.review_note = f"自动审核未发布，待人工处理：{detail}"
        if review is not None:
            draft.review_note += "；" + json.dumps(asdict(review), ensure_ascii=False)
        draft.reviewed_at = datetime.now(UTC)
        await db.commit()
        return "held"
    author_avatar_url = None
    if draft.author_avatar_url:
        try:
            author_avatar_url = await cos_service.upload_author_avatar(draft.author_avatar_url)
        except cos_service.CosUploadError as exc:
            logger.warning("草稿 %s 作者头像转存失败：%s", draft_id, exc)
    prompt = Prompt(
        title=review.suggested_title[:200],
        prompt_text=review.cleaned_prompt_text,
        category=review.suggested_category,
        models=normalize_model_labels((draft.extracted_models or "").split(",")),
        author_name=draft.author_name,
        author_avatar_url=author_avatar_url,
        source_url=draft.source_url,
        is_published=True,
    )
    db.add(prompt)
    await db.flush()
    for index, url in enumerate(credible_urls):
        db.add(PromptImage(prompt_id=prompt.id, image_url=url, thumbnail_url=url, sort_order=index))
    draft.status = "approved"
    draft.approved_prompt_id = prompt.id
    draft.reviewed_at = datetime.now(UTC)
    draft.review_note = "自动审核通过并发布：" + json.dumps(asdict(review), ensure_ascii=False)
    await db.commit()
    return "published"
