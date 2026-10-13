"""One scheduled crawl/review cycle. Run: python -m scripts.scheduled_crawl.

Scheduling/timeouts belong to the server's systemd timer/service. Never reviews
historical pending drafts; the caller must hold crawl_lock for the whole cycle.
"""

import argparse
import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.database import AsyncSessionLocal, engine
from app.models import CrawlDraft, CrawlTask
from app.services.auto_review_service import auto_publish_draft
from app.services.crawl_lock import crawl_lock
from app.services.crawl_task_service import _run_single_platform_task


async def _draft_ids(db, platform="xiaohongshu"):
    result = await db.execute(select(CrawlDraft.id).where(CrawlDraft.source_platform == platform))
    return set(result.scalars().all())


async def execute_persisted_task(task_id, auto_publish=False):
    async with AsyncSessionLocal() as db:
        task = await db.get(CrawlTask, task_id)
        if task is None:
            return {}
        platform = getattr(task, "source_platform", "xiaohongshu")
        before = await _draft_ids(db) if platform == "xiaohongshu" else await _draft_ids(db, platform)
    await _run_single_platform_task(task_id, await_review=auto_publish)
    counts = {"published": 0, "held": 0, "skipped": 0}
    async with AsyncSessionLocal() as db:
        task = await db.get(CrawlTask, task_id)
        if task.status not in ({"reviewing"} if auto_publish else {"success"}):
            return counts
        new_ids = (
            await _draft_ids(db) if platform == "xiaohongshu" else await _draft_ids(db, platform)
        ) - before
        if auto_publish:
            task.status = "reviewing"
            task.finished_at = None
            task.stage = "AI 审核中"
            await db.commit()
    errors = []
    held_reasons = []
    if auto_publish:
        for draft_id in sorted(new_ids, key=str):
            try:
                async with AsyncSessionLocal() as db:
                    outcome = await auto_publish_draft(db, draft_id)
                    counts[outcome] += 1
                    if outcome == "held":
                        draft = await db.get(CrawlDraft, draft_id)
                        reason = (getattr(draft, "review_note", None) or "未通过审核").split("；")[0][:180]
                        held_reasons.append(reason)
                        if any(
                            name in reason
                            for name in ("ContentReviewError", "CosUploadError", "RuntimeError")
                        ):
                            errors.append(reason)
            except Exception as exc:
                counts["held"] += 1
                reason = f"审核处理异常：{type(exc).__name__}"
                errors.append(reason)
                logging.warning("Review task %s draft %s: %s", task_id, draft_id, type(exc).__name__)
                async with AsyncSessionLocal() as db:
                    draft = await db.get(CrawlDraft, draft_id)
                    if draft is not None:
                        draft.review_note = reason
                        await db.commit()
            async with AsyncSessionLocal() as db:
                task = await db.get(CrawlTask, task_id)
                task.published_count = counts["published"]
                task.held_count = counts["held"]
                await db.commit()
    async with AsyncSessionLocal() as db:
        task = await db.get(CrawlTask, task_id)
        task.published_count = counts["published"]
        task.held_count = counts["held"]
        all_failed = bool(new_ids) and len(errors) == len(new_ids) and counts["published"] == 0
        task.status = "failed" if all_failed else "success"
        task.error_message = (
            ("审核失败：" if all_failed else "部分审核失败：") + "；".join(errors[:3])
            if errors
            else ("内容留待人工处理：" + "；".join(held_reasons[:3]) if held_reasons else None)
        )
        task.stage = "审核失败" if all_failed else ("完成" if new_ids else "本轮无新增内容")
        task.finished_at = datetime.now(UTC)
        task.log_tail = ((task.log_tail or "") + "\n自动审核结果：" + json.dumps(counts))[-64000:]
        await db.commit()
    return counts


async def run_once(keyword: str, limit: int, platform="xiaohongshu") -> dict:
    from app.services.crawl_task_service import PLATFORM_SPECS

    if not keyword.strip() or len(keyword) > 200 or not 1 <= limit <= 50 or platform not in PLATFORM_SPECS:
        raise ValueError(
            "keyword must contain 1-200 characters; limit must be 1-50; platform must be supported"
        )
    async with AsyncSessionLocal() as db:
        task = CrawlTask(
            id=uuid.uuid4(),
            batch_id=uuid.uuid4(),
            source_platform=platform,
            keyword=keyword.strip(),
            limit_count=limit,
            status="pending",
        )
        db.add(task)
        await db.commit()
        task_id = task.id
    counts = await execute_persisted_task(task_id, auto_publish=True)
    async with AsyncSessionLocal() as db:
        task = await db.get(CrawlTask, task_id)
        if task.status != "success":
            raise RuntimeError(f"采集任务 {task_id} 失败，请在后台查看任务日志")
    return counts


async def main(keyword: str, limit: int):
    try:
        async with crawl_lock() as acquired:
            if not acquired:
                logging.info("已有采集任务运行，本轮跳过")
                return
            print(json.dumps(await run_once(keyword, limit), ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="AI绘画提示词")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(args.keyword, args.limit))
