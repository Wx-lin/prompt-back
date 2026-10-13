import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def test_only_new_drafts_are_reviewed(monkeypatch):
    from scripts import scheduled_crawl as svc

    db = Mock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(status="reviewing", log_tail="crawl log"))

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(svc, "AsyncSessionLocal", session)
    monkeypatch.setattr(svc, "_draft_ids", AsyncMock(side_effect=[{"old"}, {"old", "new"}]))
    run = AsyncMock()
    review = AsyncMock(return_value="published")
    monkeypatch.setattr(svc, "_run_single_platform_task", run)
    monkeypatch.setattr(svc, "auto_publish_draft", review)
    assert asyncio.run(svc.run_once("AI绘画提示词", 20)) == {"published": 1, "held": 0, "skipped": 0}
    assert review.await_args.args == (db, "new")
    run.assert_awaited_once()
    assert run.await_args.kwargs == {"await_review": True}


def test_failed_crawl_does_not_review(monkeypatch):
    from scripts import scheduled_crawl as svc

    db = Mock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(status="failed"))

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(svc, "AsyncSessionLocal", session)
    monkeypatch.setattr(svc, "_draft_ids", AsyncMock(return_value={"old"}))
    monkeypatch.setattr(svc, "_run_single_platform_task", AsyncMock())
    review = AsyncMock()
    monkeypatch.setattr(svc, "auto_publish_draft", review)
    import pytest

    with pytest.raises(RuntimeError):
        asyncio.run(svc.run_once("AI", 20))
    review.assert_not_awaited()


def test_main_skips_when_another_crawl_holds_lock(monkeypatch):
    from scripts import scheduled_crawl as svc

    @asynccontextmanager
    async def locked():
        yield False

    monkeypatch.setattr(svc, "crawl_lock", locked)
    run = AsyncMock()
    monkeypatch.setattr(svc, "run_once", run)
    monkeypatch.setattr(svc, "engine", Mock(dispose=AsyncMock()))
    asyncio.run(svc.main("AI", 20))
    run.assert_not_awaited()


def test_lock_releases_even_on_failure(monkeypatch):
    import pytest

    from app.services import crawl_lock as svc

    connection = Mock()
    connection.execute = AsyncMock(return_value=Mock(scalar_one=Mock(return_value=True)))
    connection.commit = AsyncMock()

    @asynccontextmanager
    async def connect():
        yield connection

    engine = Mock()
    engine.connect = connect
    engine.dispose = AsyncMock()
    monkeypatch.setattr(svc, "create_async_engine", Mock(return_value=engine))

    async def run():
        async with svc.crawl_lock() as acquired:
            assert acquired
            raise ValueError("work failed")

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert "pg_advisory_unlock" in str(connection.execute.await_args.args[0])
    engine.dispose.assert_awaited_once()


def test_review_stage_and_exception_are_truthful(monkeypatch):
    from app.models import CrawlTask
    from scripts import scheduled_crawl as svc

    task = SimpleNamespace(status="reviewing", log_tail="", source_platform="xiaohongshu")
    draft = SimpleNamespace(review_note=None)
    db = Mock()
    db.commit = AsyncMock()

    async def get(model, id):
        return task if model is CrawlTask else draft

    db.get = get

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(svc, "AsyncSessionLocal", session)
    monkeypatch.setattr(svc, "_draft_ids", AsyncMock(side_effect=[set(), {"new"}]))
    monkeypatch.setattr(svc, "_run_single_platform_task", AsyncMock())

    async def audit(db, id):
        assert task.status == "reviewing"
        assert task.finished_at is None
        raise RuntimeError("private details must not leak")

    monkeypatch.setattr(svc, "auto_publish_draft", audit)
    assert asyncio.run(svc.execute_persisted_task("task", True))["held"] == 1
    assert task.status == "failed"
    assert "审核失败" in task.error_message
    assert "RuntimeError" in draft.review_note


def test_review_persists_counts_before_processing_next_draft(monkeypatch):
    from scripts import scheduled_crawl as svc

    task = SimpleNamespace(
        status="reviewing", log_tail="", source_platform="xiaohongshu", published_count=0, held_count=0
    )
    db = Mock(get=AsyncMock(return_value=task), commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(svc, "AsyncSessionLocal", session)
    monkeypatch.setattr(svc, "_draft_ids", AsyncMock(side_effect=[set(), {"a", "b"}]))
    monkeypatch.setattr(svc, "_run_single_platform_task", AsyncMock())

    async def review(db, draft_id):
        if draft_id == "b":
            assert task.status == "reviewing"
            assert task.published_count == 1
            assert task.finished_at is None
        return "published"

    monkeypatch.setattr(svc, "auto_publish_draft", review)
    counts = asyncio.run(svc.execute_persisted_task("task", True))
    assert counts["published"] == 2
    assert task.status == "success"


def test_empty_round_reports_no_new_content(monkeypatch):
    from scripts import scheduled_crawl as svc

    task = SimpleNamespace(status="reviewing", log_tail="crawl log", source_platform="xiaohongshu")
    db = Mock(get=AsyncMock(return_value=task), commit=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(svc, "AsyncSessionLocal", session)
    monkeypatch.setattr(svc, "_draft_ids", AsyncMock(side_effect=[{"old"}, {"old"}]))
    monkeypatch.setattr(svc, "_run_single_platform_task", AsyncMock())
    review = AsyncMock()
    monkeypatch.setattr(svc, "auto_publish_draft", review)
    assert asyncio.run(svc.execute_persisted_task("task", True)) == {"published": 0, "held": 0, "skipped": 0}
    assert task.status == "success"
    assert task.stage == "本轮无新增内容"
    review.assert_not_awaited()
