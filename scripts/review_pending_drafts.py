"""
一次性脚本：批量审核草稿（CrawlDraft，status=pending）并转为正式内容。

处理顺序（和 approve_draft 的"先审后传"权宜顺序不同，这里采用更合理的顺序，
因为是批量脚本不受线上接口的历史实现约束）：
  1. 把草稿每张图片转存到 COS，拿到公网 URL
     - 本地磁盘路径（MediaCrawler 落盘）：用 cos_service.upload_prompt_image
     - 已经是第三方 CDN 直链（如 xhscdn.com）：用 cos_service.migrate_remote_image_to_cos
     - 已经是 COS 地址：跳过
  2. 用全部图片的公网 URL + 原始文案调用 content_review_service 做 AI 预审
  3. 至少一张图片可信 → 创建正式 Prompt（只保留可信图片），is_published=True，
     草稿 status 改为 approved
  4. 全部图片不可信 → 不创建内容，草稿保持 pending，打日志提醒人工复核

用法：uv run python -m scripts.review_pending_drafts
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CrawlDraft, Prompt, PromptImage
from app.services import content_review_service, copyright_filter, cos_service
from app.utils.category_catalog import CATEGORY_LABELS

logging.basicConfig(level=logging.WARNING)  # 屏蔽 sqlalchemy 的 INFO 日志，保留我们自己的 WARNING
settings = get_settings()

_MEDIACRAWLER_PATH_PREFIXES = ("xhs/media/", "wb/media/", "ks/media/")


def _resolve_local_file_path(local_path: str) -> Path | None:
    """和 crawl_service._resolve_local_file_path 保持一致的解析规则。"""
    if local_path.startswith("http"):
        return None
    if local_path.startswith(_MEDIACRAWLER_PATH_PREFIXES):
        mediacrawler_dir = Path(settings.mediacrawler_dir).expanduser().resolve()
        return mediacrawler_dir / "data" / local_path
    return Path(settings.crawl_media_dir).expanduser().resolve() / local_path


async def _ensure_public_url(local_path: str) -> str | None:
    """
    把草稿图片的 local_path 转成公网 URL：
    - 已经是 COS 地址：原样返回
    - 已经是其他 http(s) 直链（如 xhscdn）：迁移到 COS 后返回新地址
    - 本地磁盘路径：上传到 COS 后返回新地址

    任何失败都返回 None，调用方跳过这张图，不让单张坏图挡住整条草稿。
    """
    if "cos.ap-guangzhou" in local_path:
        return local_path
    if local_path.startswith("http"):
        try:
            return await cos_service.migrate_remote_image_to_cos(local_path)
        except cos_service.CosUploadError as exc:
            print(f"    ⚠️ 迁移远程图片失败: {exc}")
            return None

    local_file = _resolve_local_file_path(local_path)
    if local_file is None or not local_file.exists():
        print(f"    ⚠️ 本地文件不存在，跳过: {local_path}")
        return None
    try:
        return await cos_service.upload_prompt_image(local_file)
    except cos_service.CosUploadError as exc:
        print(f"    ⚠️ 上传本地图片失败: {exc}")
        return None


async def main() -> None:
    async with AsyncSessionLocal() as db:
        query = (
            select(CrawlDraft)
            .where(CrawlDraft.status == "pending")
            .options(selectinload(CrawlDraft.images))
            .order_by(CrawlDraft.created_at)
        )
        drafts = (await db.execute(query)).scalars().all()
        draft_ids = [d.id for d in drafts]

    print(f"共 {len(draft_ids)} 条待审核草稿，开始处理...\n")

    published = 0
    kept_pending = 0
    failed = 0
    copyright_blocked = 0

    for i, draft_id in enumerate(draft_ids, 1):
        async with AsyncSessionLocal() as db:
            query = (
                select(CrawlDraft).where(CrawlDraft.id == draft_id).options(selectinload(CrawlDraft.images))
            )
            draft = (await db.execute(query)).scalar_one()

            title = draft.raw_title or draft.raw_content[:20]
            print(f"[{i}/{len(draft_ids)}] {title}")

            # 第零步：版权声明硬拦截，命中直接跳过，不进入图片转存和 AI 审核
            # （避免浪费 COS 上传和模型调用成本在明确不能发布的内容上）
            raw_content = draft.raw_content or ""
            hit_keyword = copyright_filter.contains_copyright_restriction(
                raw_content
            ) or copyright_filter.contains_copyright_restriction(
                copyright_filter.strip_emoji_and_punctuation(raw_content)
            )
            if hit_keyword:
                print(f"    🚫 命中版权限制声明关键词「{hit_keyword}」，保持 pending，不处理\n")
                copyright_blocked += 1
                continue

            # 第一步：把每张图片转存到公网 URL
            resolved_urls: list[tuple[str, int]] = []  # (url, sort_order)
            images_sorted = sorted(draft.images, key=lambda img: img.sort_order)
            for img in images_sorted:
                url = await _ensure_public_url(img.local_path)
                if url:
                    resolved_urls.append((url, img.sort_order))

            if not resolved_urls:
                print("    ⚠️ 没有任何图片成功转存，跳过，保持 pending\n")
                failed += 1
                continue

            # 第二步：AI 预审
            try:
                review = await content_review_service.review_draft_content(
                    raw_text=draft.raw_content or "",
                    image_urls=[url for url, _ in resolved_urls],
                )
            except content_review_service.ContentReviewError as exc:
                print(f"    ❌ 审核失败，跳过，保持 pending: {exc}\n")
                failed += 1
                continue

            if review.suggested_category not in CATEGORY_LABELS:
                print("分类用途不明确，保持原状态，交人工审核")
                kept_pending += 1
                continue

            if not review.has_credible_image:
                print("    ⚠️ 全部图片不可信，保持 pending，不创建内容\n")
                kept_pending += 1
                continue

            # 第三步：创建正式内容，只保留可信图片
            credible_indices = {img.index for img in review.images if img.is_credible}
            kept_images = [
                (url, sort_order)
                for idx, (url, sort_order) in enumerate(resolved_urls)
                if idx in credible_indices
            ]

            title_final = review.suggested_title or draft.raw_title or "未命名"
            prompt_text_final = review.cleaned_prompt_text or draft.extracted_prompt_text or draft.raw_content
            category_final = review.suggested_category
            raw_models = draft.extracted_models or ""
            models_final = [m.strip() for m in raw_models.split(",") if m.strip()]

            prompt = Prompt(
                title=title_final,
                prompt_text=prompt_text_final,
                category=category_final,
                models=models_final,
                author_name=draft.author_name,
                source_url=draft.source_url,
                is_published=True,
            )
            db.add(prompt)
            await db.flush()

            for url, sort_order in kept_images:
                db.add(
                    PromptImage(
                        prompt_id=prompt.id,
                        image_url=url,
                        thumbnail_url=url,
                        sort_order=sort_order,
                    )
                )

            draft.status = "approved"
            draft.reviewed_at = datetime.now()
            draft.approved_prompt_id = prompt.id

            await db.commit()
            published += 1
            print(
                f"    ✅ 已发布：标题={title_final!r} 分类={category_final} "
                f"保留 {len(kept_images)}/{len(resolved_urls)} 张图片\n"
            )

    print("===== 汇总 =====")
    print(f"成功发布: {published}")
    print(f"全部图片不可信（保持 pending）: {kept_pending}")
    print(f"命中版权限制声明（保持 pending）: {copyright_blocked}")
    print(f"处理失败（保持 pending）: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
