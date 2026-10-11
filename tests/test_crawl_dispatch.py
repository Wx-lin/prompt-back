"""Dispatcher persistence, idempotence and interrupted execution recovery."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CrawlSchedule, CrawlTask
from scripts import crawl_dispatch as dispatch


@pytest_asyncio.fixture
async def store(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: CrawlSchedule.__table__.create(c))
        await connection.run_sync(lambda c: CrawlTask.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dispatch, "AsyncSessionLocal", factory)

    @asynccontextmanager
    async def acquired():
        yield True

    monkeypatch.setattr(dispatch, "crawl_lock", acquired)
    yield factory
    await engine.dispose()


async def schedule(store, now):
    async with store() as db:
        value = CrawlSchedule(
            name="Daily",
            platforms=["weibo"],
            keyword="AI",
            times=["10:00"],
            enabled=True,
            auto_publish=True,
            next_run_at=now,
        )
        db.add(value)
        await db.commit()
        return value.id


@pytest.mark.asyncio
async def test_due_runs_once_then_future(store, monkeypatch):
    now = datetime.now(UTC)
    identifier = await schedule(store, now)
    execute = AsyncMock()
    monkeypatch.setattr(dispatch, "execute_persisted_task", execute)
    assert await dispatch.dispatch_due(now) == 1
    assert await dispatch.dispatch_due(now) == 0
    async with store() as db:
        tasks = list(await db.scalars(select(CrawlTask)))
        assert len(tasks) == 1
        assert tasks[0].schedule_id == identifier
        assert tasks[0].trigger_type == "scheduled"
        assert tasks[0].scheduled_for.replace(tzinfo=UTC) == now
        setting = await db.get(CrawlSchedule, identifier)
        assert setting.next_run_at.replace(tzinfo=UTC) > now
    execute.assert_awaited_once_with(tasks[0].id, True)


@pytest.mark.asyncio
async def test_locked_dispatch_leaves_due_schedule_unchanged(store, monkeypatch):
    now = datetime.now(UTC)
    identifier = await schedule(store, now)

    @asynccontextmanager
    async def unavailable():
        yield False

    monkeypatch.setattr(dispatch, "crawl_lock", unavailable)
    assert await dispatch.dispatch_due(now) == 0
    async with store() as db:
        value = await db.get(CrawlSchedule, identifier)
        assert value.next_run_at.replace(tzinfo=UTC) == now
        assert list(await db.scalars(select(CrawlTask))) == []


@pytest.mark.asyncio
async def test_orphan_failure_and_pending_resume(store, monkeypatch):
    now = datetime.now(UTC)
    async with store() as db:
        tasks = [
            CrawlTask(
                id=uuid.uuid4(),
                batch_id=uuid.uuid4(),
                source_platform="weibo",
                keyword="AI",
                limit_count=2,
                status=status,
                created_at=now - timedelta(minutes=5),
            )
            for status in ["running", "awaiting_login", "reviewing", "pending"]
        ]
        db.add_all(tasks)
        await db.commit()
    execute = AsyncMock()
    monkeypatch.setattr(dispatch, "execute_persisted_task", execute)
    assert await dispatch.dispatch_due(now) == 1
    execute.assert_awaited_once_with(tasks[-1].id, False)
    async with store() as db:
        for original in tasks[:-1]:
            task = await db.get(CrawlTask, original.id)
            assert task.status == "failed"
            assert task.stage == "执行中断"
            assert task.finished_at is not None


@pytest.mark.asyncio
async def test_runtime_exception_is_persisted_and_next_task_continues(store, monkeypatch):
    async with store() as db:
        tasks = [
            CrawlTask(
                id=uuid.uuid4(),
                batch_id=uuid.uuid4(),
                source_platform="weibo",
                keyword="AI",
                limit_count=2,
                status="pending",
            )
            for _ in range(2)
        ]
        db.add_all(tasks)
        await db.commit()
    execute = AsyncMock(side_effect=[RuntimeError("secret must not be shown"), None])
    monkeypatch.setattr(dispatch, "execute_persisted_task", execute)
    await dispatch._execute([(t.id, False) for t in tasks])
    assert execute.await_count == 2
    async with store() as db:
        failed = await db.get(CrawlTask, tasks[0].id)
        assert failed.status == "failed"
        assert "RuntimeError" in failed.error_message
        assert "secret" not in failed.error_message


@pytest.mark.asyncio
async def test_manual_locked_keeps_queue_for_dispatch(store, monkeypatch):
    async with store() as db:
        task = CrawlTask(
            id=uuid.uuid4(),
            batch_id=uuid.uuid4(),
            source_platform="weibo",
            keyword="AI",
            limit_count=2,
            status="pending",
        )
        db.add(task)
        await db.commit()

    @asynccontextmanager
    async def unavailable():
        yield False

    monkeypatch.setattr(dispatch, "crawl_lock", unavailable)
    await dispatch._manual_execute([(task.id, False)])
    async with store() as db:
        queued = await db.get(CrawlTask, task.id)
        assert queued.status == "pending"
        assert queued.stage == "排队等待其他采集任务"


@pytest.mark.asyncio
async def test_create_tasks_duplicate_slot_is_skipped(store):
    now = datetime.now(UTC)
    identifier = await schedule(store, now)
    async with store() as db:
        setting = await db.get(CrawlSchedule, identifier)
        assert len(await dispatch._create_tasks(db, setting, now, "scheduled")) == 1
        await db.commit()
        assert await dispatch._create_tasks(db, setting, now, "scheduled") == []


@pytest.mark.asyncio
async def test_manual_enqueue_missing_and_persisted(store, monkeypatch):
    with pytest.raises(ValueError):
        await dispatch.enqueue_schedule(uuid.uuid4())
    identifier = await schedule(store, datetime.now(UTC))
    execute = AsyncMock()
    monkeypatch.setattr(dispatch, "_manual_execute", execute)
    tasks = await dispatch.enqueue_schedule(identifier)
    import asyncio

    await asyncio.sleep(0)
    assert len(tasks) == 1
    assert tasks[0].trigger_type == "manual"
    execute.assert_awaited_once()
