from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.crawl_schedule import CrawlScheduleCreate
from app.services.crawl_schedule_service import next_run


def test_next_run_uses_shanghai_and_rolls_day():
    now = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
    assert next_run(["10:00", "22:00"], now) == datetime(2026, 10, 11, 2, 0, tzinfo=UTC)


def test_schedule_normalizes_times_and_platforms():
    value = CrawlScheduleCreate(
        name="测试", platforms=["weibo", "weibo"], keyword="AI", times=["22:00", "10:00", "22:00"]
    )
    assert value.times == ["10:00", "22:00"]
    assert value.platforms == ["weibo"]


@pytest.mark.parametrize(
    "field,value", [("times", ["25:00"]), ("platforms", ["unknown"]), ("limit_count", 51), ("keyword", "   ")]
)
def test_schedule_rejects_invalid_boundary(field, value):
    payload = dict(name="test", platforms=["weibo"], keyword="AI", times=["10:00"])
    payload[field] = value
    with pytest.raises(ValidationError):
        CrawlScheduleCreate(**payload)


@pytest.mark.asyncio
async def test_schedule_api_crud_and_history(monkeypatch, tmp_path):
    import uuid

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import get_db
    from app.models import CrawlSchedule, CrawlTask
    from app.routers.crawl_operations import router
    from app.services import crawl_login_service
    from app.utils.admin_auth import verify_admin_key

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: CrawlSchedule.__table__.create(c))
        await conn.run_sync(lambda c: CrawlTask.__table__.create(c))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)

    async def database():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/admin/crawl-schedules")).status_code == 401
        assert (await client.get(f"/api/admin/crawl-tasks/{uuid.uuid4()}/login-qr")).status_code == 401
        app.dependency_overrides[verify_admin_key] = lambda: None
        result = await client.post(
            "/api/admin/crawl-schedules",
            json=dict(name="Daily", platforms=["weibo"], keyword="AI", times=["10:00"]),
        )
        assert result.status_code == 201, result.text
        schedule_id = result.json()["id"]
        assert result.json()["next_run_at"] is not None
        result = await client.patch(f"/api/admin/crawl-schedules/{schedule_id}", json={"enabled": False})
        assert result.status_code == 200, result.text
        assert result.json()["next_run_at"] is None
        assert len((await client.get("/api/admin/crawl-schedules")).json()) == 1
        async with factory() as db:
            db.add(
                CrawlTask(
                    batch_id=uuid.uuid4(),
                    source_platform="weibo",
                    keyword="AI",
                    limit_count=3,
                    status="failed",
                    error_message="session expired",
                )
            )
            await db.commit()
        result = await client.get("/api/admin/crawl-runs?status=failed")
        assert result.status_code == 200, result.text
        assert result.json()["summary"]["failed"] == 1
        task_id = result.json()["items"][0]["id"]
        assert (await client.post(f"/api/admin/crawl-tasks/{task_id}/login/refresh")).status_code == 409
        assert (await client.get(f"/api/admin/crawl-tasks/{task_id}/login-qr")).status_code == 404
        png = tmp_path / "qr.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(crawl_login_service, "login_image_path", lambda _: png)
        async with factory() as db:
            task = await db.get(CrawlTask, uuid.UUID(task_id))
            task.status = "awaiting_login"
            await db.commit()
        metadata = dict(
            state="awaiting_login", message="scan", expires_at="2000-01-01T00:00:00+00:00", qr_version="v1"
        )
        monkeypatch.setattr(crawl_login_service, "get_login_metadata", lambda _: metadata)
        result = await client.get(f"/api/admin/crawl-tasks/{task_id}/login")
        assert result.json()["qr_available"] is False
        assert (await client.get(f"/api/admin/crawl-tasks/{task_id}/login-qr")).status_code == 404
        metadata = {**metadata, "expires_at": "2999-01-01T00:00:00+00:00"}
        result = await client.get(f"/api/admin/crawl-tasks/{task_id}/login")
        assert result.json()["qr_version"] == "v1"
        assert result.json()["qr_available"] is True
        result = await client.get(f"/api/admin/crawl-tasks/{task_id}/login-qr")
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        missing = uuid.uuid4()
        assert (
            await client.patch(f"/api/admin/crawl-schedules/{missing}", json={"enabled": True})
        ).status_code == 404
        assert (await client.get(f"/api/admin/crawl-tasks/{missing}/login")).status_code == 404
        assert (await client.get("/api/admin/crawl-runs?status=unknown")).status_code == 422
        assert (
            await client.patch(f"/api/admin/crawl-schedules/{schedule_id}", json={"enabled": None})
        ).status_code == 422
        assert (await client.get(f"/api/admin/crawl-runs?date=2000-01-01&schedule_id={schedule_id}")).json()[
            "total"
        ] == 0
        monkeypatch.setattr(crawl_login_service, "request_login_refresh", lambda _: None)
        assert (await client.post(f"/api/admin/crawl-tasks/{task_id}/login/refresh")).status_code == 200

        def unavailable(_):
            raise ValueError("刷新暂不可用")

        monkeypatch.setattr(crawl_login_service, "request_login_refresh", unavailable)
        assert (await client.post(f"/api/admin/crawl-tasks/{task_id}/login/refresh")).status_code == 409
        async with factory() as db:
            task = await db.get(CrawlTask, uuid.UUID(task_id))
            task.status = "success"
            await db.commit()
        result = await client.get(f"/api/admin/crawl-tasks/{task_id}/login")
        assert result.json()["required"] is False
        assert result.json()["qr_version"] is None
        from unittest.mock import AsyncMock

        from scripts import crawl_dispatch

        monkeypatch.setattr(crawl_dispatch, "enqueue_schedule", AsyncMock(return_value=[]))
        assert (await client.post(f"/api/admin/crawl-schedules/{schedule_id}/run")).json() == []
    await engine.dispose()


@pytest.mark.parametrize(
    "metadata",
    [
        dict(state="unavailable"),
        dict(state="awaiting_login", qr_version="v1"),
        dict(state="awaiting_login", qr_version="v1", expires_at="bad"),
        dict(state="awaiting_login", qr_version="v1", expires_at="2999-01-01T00:00:00"),
    ],
)
def test_qr_metadata_invalid_fail_closed(metadata):
    from app.routers.crawl_operations import valid_qr_metadata

    assert valid_qr_metadata(metadata) is False


def test_next_run_requires_aware_time_and_slots():
    with pytest.raises(ValueError):
        next_run(["10:00"], datetime(2026, 10, 10))
    with pytest.raises(ValueError):
        next_run([], datetime(2026, 10, 10, tzinfo=UTC))


@pytest.mark.asyncio
async def test_handler_error_contracts_directly(monkeypatch):
    """Exercise handler exceptions explicitly, independently of FastAPI's compiled wrappers."""
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.routers import crawl_operations as api
    from app.services import crawl_login_service
    from scripts import crawl_dispatch

    db = SimpleNamespace(scalar=AsyncMock(return_value=None), get=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as failure:
        await api.require_schedule(uuid.uuid4(), db)
    assert failure.value.status_code == 404
    with pytest.raises(HTTPException):
        await api.require_task(uuid.uuid4(), db)
    schedule = SimpleNamespace(id=uuid.uuid4())
    db.scalar.return_value = schedule
    monkeypatch.setattr(api.service, "save_schedule", AsyncMock(side_effect=ValueError("invalid")))
    from app.schemas.crawl_schedule import CrawlScheduleUpdate

    with pytest.raises(HTTPException) as failure:
        await api.update(schedule.id, CrawlScheduleUpdate(enabled=None), db)
    assert failure.value.status_code == 422
    monkeypatch.setattr(crawl_dispatch, "enqueue_schedule", AsyncMock(return_value=[]))
    assert await api.run(schedule.id, db) == []
    task = SimpleNamespace(status="success", error_message=None)
    db.get.return_value = task
    assert (await api.login(uuid.uuid4(), db))["required"] is False
    task.status = "awaiting_login"
    monkeypatch.setattr(crawl_login_service, "get_login_metadata", lambda _: {"state": "unavailable"})
    result = await api.login(uuid.uuid4(), db)
    assert result["qr_available"] is False
    with pytest.raises(HTTPException):
        await api.login_qr(uuid.uuid4(), db)
    monkeypatch.setattr(crawl_login_service, "request_login_refresh", lambda _: None)
    assert (await api.refresh(uuid.uuid4(), db))["message"]
