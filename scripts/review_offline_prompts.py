"""
一次性脚本：

第一步：把下线数据（is_published=False）里还停留在第三方 CDN（xhscdn 等）的
        图片迁移到 COS，更新 PromptImage.image_url / thumbnail_url。
第二步：对全部下线数据跑一遍 AI 内容预审（content_review_service），只打印
        结果，不自动改 category / is_published——人工看完效果再决定下一步。

用法：uv run python scripts/review_offline_prompts.py
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import Prompt
from app.services import content_review_service, cos_service


async def migrate_non_cos_images() -> None:
    """把还没转存到 COS 的下线数据图片迁移过去，就地更新数据库。"""
    async with AsyncSessionLocal() as db:
        query = (
            select(Prompt)
            .where(Prompt.is_published.is_(False))
            .options(selectinload(Prompt.images))
        )
        prompts = (await db.execute(query)).scalars().all()

        migrated_count = 0
        failed_count = 0
        for prompt in prompts:
            for img in prompt.images:
                if "cos.ap-guangzhou" in img.image_url:
                    continue
                try:
                    new_url = await cos_service.migrate_remote_image_to_cos(img.image_url)
                    img.image_url = new_url
                    img.thumbnail_url = new_url
                    migrated_count += 1
                except cos_service.CosUploadError as exc:
                    failed_count += 1
                    print(f"  ⚠️ 迁移失败 prompt={prompt.id} url={img.image_url[:80]}: {exc}")

        await db.commit()
        print(f"图片迁移完成：成功 {migrated_count} 张，失败 {failed_count} 张\n")


async def review_all() -> None:
    async with AsyncSessionLocal() as db:
        query = (
            select(Prompt)
            .where(Prompt.is_published.is_(False))
            .options(selectinload(Prompt.images))
            .order_by(Prompt.created_at)
        )
        prompts = (await db.execute(query)).scalars().all()

    print(f"共 {len(prompts)} 条下线数据，开始逐条 AI 审核...\n")

    for i, prompt in enumerate(prompts, 1):
        image_urls = [img.image_url for img in prompt.images]
        print(f"[{i}/{len(prompts)}] {prompt.id}")
        print(f"  原标题: {prompt.title}")
        print(f"  原分类: {prompt.category}")
        print(f"  图片数: {len(image_urls)}")

        try:
            review = await content_review_service.review_draft_content(
                raw_text=prompt.prompt_text or "",
                image_urls=image_urls,
            )
        except content_review_service.ContentReviewError as exc:
            print(f"  ❌ 审核失败: {exc}\n")
            continue

        for img in review.images:
            mark = "✅可信" if img.is_credible else "❌不可信"
            print(f"    图{img.index}: {mark} - {img.reason}")
        print(f"  建议标题: {review.suggested_title}")
        print(f"  建议分类: {review.suggested_category}")
        print(f"  清洗后文案: {review.cleaned_prompt_text}")
        conclusion = "建议保留" if review.has_credible_image else "⚠️ 全部图片不可信，建议继续下线或人工复核"
        print(f"  整体结论: {conclusion}")
        print()


async def main() -> None:
    print("===== 第一步：迁移未入 COS 的图片 =====\n")
    await migrate_non_cos_images()
    print("===== 第二步：AI 内容预审 =====\n")
    await review_all()


if __name__ == "__main__":
    asyncio.run(main())
