"""
一次性脚本：在 review_offline_prompts.py 跑完 AI 预审基础上，把结果正式应用到数据库：

- 对"至少一张图片可信"的条目：
  - 用 AI 建议的 title / prompt_text / category 覆盖原值
  - 删除被判定为不可信的图片（PromptImage），只保留可信的
  - is_published 设为 True（重新上线）
- 对"全部图片不可信"的条目：保持 is_published=False，不改动，留给人工复核

执行前会重新跑一次 AI 审核（而不是复用上次脚本的打印结果），确保"审核判断"和
"写库动作"在同一次调用里，不会因为复制粘贴判断结果出现人为错配。

用法：uv run python -m scripts.apply_offline_review
"""

import asyncio

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models import Prompt, PromptImage
from app.services import content_review_service
from app.utils.category_catalog import CATEGORY_LABELS


async def main() -> None:
    async with AsyncSessionLocal() as db:
        query = (
            select(Prompt)
            .where(Prompt.is_published.is_(False))
            .options(selectinload(Prompt.images))
            .order_by(Prompt.created_at)
        )
        prompts = (await db.execute(query)).scalars().all()

    print(f"共 {len(prompts)} 条下线数据，开始审核并应用...\n")

    republished = 0
    kept_offline = 0
    failed = 0

    for i, prompt in enumerate(prompts, 1):
        async with AsyncSessionLocal() as db:
            # 重新从这个独立 session 里取一份带 images 的新鲜对象，避免跨 session 残留状态
            query = select(Prompt).where(Prompt.id == prompt.id).options(selectinload(Prompt.images))
            p = (await db.execute(query)).scalar_one()
            image_urls = [img.image_url for img in p.images]

            print(f"[{i}/{len(prompts)}] {p.title}")
            try:
                review = await content_review_service.review_draft_content(
                    raw_text=p.prompt_text or "",
                    image_urls=image_urls,
                )
            except content_review_service.ContentReviewError as exc:
                print(f"  ❌ 审核失败，跳过，保持下线: {exc}\n")
                failed += 1
                continue

            if review.suggested_category not in CATEGORY_LABELS:
                print("分类用途不明确，保持原状态，交人工审核")
                kept_offline += 1
                continue

            if not review.has_credible_image:
                print("  ⚠️ 全部图片不可信，保持下线，不改动\n")
                kept_offline += 1
                continue

            # 建立 index -> PromptImage 的映射（index 对应 image_urls 的顺序，
            # 也就是 sort_order 升序，和 review.images 的 index 语义一致）
            images_sorted = sorted(p.images, key=lambda img: img.sort_order)
            credible_indices = {img.index for img in review.images if img.is_credible}

            removed_count = 0
            for idx, img in enumerate(images_sorted):
                if idx not in credible_indices:
                    await db.execute(delete(PromptImage).where(PromptImage.id == img.id))
                    removed_count += 1

            if review.suggested_title:
                p.title = review.suggested_title
            if review.cleaned_prompt_text:
                p.prompt_text = review.cleaned_prompt_text
            if review.suggested_category:
                p.category = review.suggested_category
            p.is_published = True

            await db.commit()
            republished += 1
            print(
                f"  ✅ 已更新并上线：标题={p.title!r} 分类={p.category} "
                f"剔除不可信图片 {removed_count} 张，保留 {len(images_sorted) - removed_count} 张\n"
            )

    print("===== 汇总 =====")
    print(f"重新上线: {republished}")
    print(f"保持下线（全部图片不可信）: {kept_offline}")
    print(f"审核失败（保持下线）: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
