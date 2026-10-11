"""Database-backed minute dispatcher. Server systemd invokes this module."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal, engine
from app.models import CrawlSchedule, CrawlTask
from app.services.crawl_lock import crawl_lock
from app.services.crawl_schedule_service import next_run as compute_next_run
from scripts.scheduled_crawl import execute_persisted_task


async def _create_tasks(db, schedule, slot, trigger):
    batch = uuid.uuid4()
    tasks = []
    for platform in schedule.platforms:
        existing = (
            (
                await db.execute(
                    select(CrawlTask).where(
                        CrawlTask.schedule_id == schedule.id,
                        CrawlTask.scheduled_for == slot,
                        CrawlTask.source_platform == platform,
                    )
                )
            ).scalar_one_or_none()
            if trigger == "scheduled"
            else None
        )
        if existing:
            continue
        task = CrawlTask(
            id=uuid.uuid4(),
            batch_id=batch,
            schedule_id=schedule.id,
            scheduled_for=slot,
            trigger_type=trigger,
            source_platform=platform,
            keyword=schedule.keyword,
            limit_count=schedule.limit_count,
            status="pending",
        )
        db.add(task)
        tasks.append(task)
    await db.flush()
    return tasks


async def _execute(tasks):
    for task_id, auto_publish in tasks:
        try:
            await execute_persisted_task(task_id, auto_publish)
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                task = await db.get(CrawlTask, task_id)
                if task is not None:
                    task.status = "failed"
                    task.stage = "执行异常"
                    task.error_message = f"采集任务执行异常：{type(exc).__name__}"
                    task.finished_at = datetime.now(UTC)
                    await db.commit()


async def _manual_execute(tasks):
    async with crawl_lock() as acquired:
        if acquired:
            await _execute(tasks)
        else:
            async with AsyncSessionLocal() as db:
                for task_id, _ in tasks:
                    task = await db.get(CrawlTask, task_id)
                    task.stage = "排队等待其他采集任务"
                    task.error_message = None
                await db.commit()


async def enqueue_schedule(schedule_id):
    async with AsyncSessionLocal() as db:
        schedule = await db.get(CrawlSchedule, schedule_id)
        if not schedule:
            raise ValueError("定时任务不存在")
        tasks = await _create_tasks(db, schedule, datetime.now(UTC), "manual")
        auto_publish = schedule.auto_publish
        await db.commit()
        for task in tasks:
            await db.refresh(task)
        asyncio.create_task(_manual_execute([(task.id, auto_publish) for task in tasks]))
        return tasks


async def dispatch_due(now=None):
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=60)
    async with AsyncSessionLocal() as db:
        due = (
            await db.execute(
                select(CrawlSchedule.id)
                .where(CrawlSchedule.enabled.is_(True), CrawlSchedule.next_run_at <= now)
                .limit(1)
            )
        ).first()
        queued = (
            await db.execute(
                select(CrawlTask.id)
                .where(
                    CrawlTask.status.in_(["pending", "running", "awaiting_login", "reviewing"]),
                    CrawlTask.created_at <= cutoff,
                )
                .limit(1)
            )
        ).first()
    if not due and not queued:
        return 0
    async with crawl_lock() as acquired:
        if not acquired:
            return 0
        async with AsyncSessionLocal() as db:
            work = []
            orphaned = (
                await db.scalars(
                    select(CrawlTask)
                    .where(
                        CrawlTask.status.in_(["running", "awaiting_login", "reviewing"]),
                        CrawlTask.created_at <= cutoff,
                    )
                    .with_for_update()
                )
            ).all()
            for task in orphaned:
                task.status = "failed"
                task.stage = "执行中断"
                task.error_message = "原执行进程已中断，请重新执行；已有内容和草稿保留"
                task.finished_at = now
            pending = (
                await db.scalars(
                    select(CrawlTask)
                    .where(CrawlTask.status == "pending", CrawlTask.created_at <= cutoff)
                    .order_by(CrawlTask.created_at)
                    .with_for_update()
                )
            ).all()
            for task in pending:
                schedule = await db.get(CrawlSchedule, task.schedule_id) if task.schedule_id else None
                work.append((task.id, bool(schedule and schedule.auto_publish)))
            schedules = (
                await db.scalars(
                    select(CrawlSchedule)
                    .where(CrawlSchedule.enabled.is_(True), CrawlSchedule.next_run_at <= now)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for schedule in schedules:
                tasks = await _create_tasks(db, schedule, schedule.next_run_at, "scheduled")
                work.extend((task.id, schedule.auto_publish) for task in tasks)
                schedule.next_run_at = compute_next_run(schedule.times, now)
            await db.commit()
        await _execute(work)
        return len(work)


async def main():
    try:
        await dispatch_due()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
