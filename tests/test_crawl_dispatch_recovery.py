import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CrawlSchedule, CrawlTask
from scripts import crawl_dispatch as dispatch


def test_committed_queue_survives_restart_and_is_not_run_twice(monkeypatch, tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/queue.db")
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: CrawlSchedule.__table__.create(c))
            await conn.run_sync(lambda c: CrawlTask.__table__.create(c))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 10, 10, 3, tzinfo=UTC)
        async with sessions() as db:
            schedule = CrawlSchedule(
                id=uuid.uuid4(),
                name="test",
                platforms=["xiaohongshu"],
                keyword="AI",
                times=["22:00"],
                limit_count=3,
                enabled=True,
                auto_publish=True,
                next_run_at=now + timedelta(hours=11),
            )
            db.add(schedule)
            await db.flush()
            task = CrawlTask(
                id=uuid.uuid4(),
                batch_id=uuid.uuid4(),
                schedule_id=schedule.id,
                scheduled_for=now - timedelta(hours=1),
                trigger_type="scheduled",
                source_platform="xiaohongshu",
                keyword="AI",
                limit_count=3,
                status="pending",
                created_at=now - timedelta(minutes=2),
            )
            db.add(task)
            await db.commit()
            task_id = task.id

        @asynccontextmanager
        async def locked():
            yield True

        seen = []

        async def execute(id, auto):
            seen.append(id)
            async with sessions() as db:
                row = await db.get(CrawlTask, id)
                row.status = "success"
                await db.commit()

        monkeypatch.setattr(dispatch, "AsyncSessionLocal", sessions)
        monkeypatch.setattr(dispatch, "crawl_lock", locked)
        monkeypatch.setattr(dispatch, "execute_persisted_task", execute)
        assert await dispatch.dispatch_due(now) == 1
        assert seen == [task_id]
        assert await dispatch.dispatch_due(now) == 0
        assert seen == [task_id]
        async with sessions() as db:
            assert len((await db.scalars(select(CrawlTask))).all()) == 1
        await engine.dispose()

    asyncio.run(run())
