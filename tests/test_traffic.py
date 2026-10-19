import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.schemas.traffic import VisitInput
from app.services import traffic_service as service


def test_strict_payload():
    from pydantic import ValidationError

    for payload in (
        {"visitor_hash": "a" * 63},
        {"visitor_hash": "g" * 64},
        {"visitor_hash": "a" * 64, "ip": "private"},
        {"visitor_hash": 123},
    ):
        with pytest.raises(ValidationError):
            VisitInput.model_validate(payload)
    assert VisitInput(visitor_hash="A" * 64).visitor_hash == "a" * 64


@pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit isolated PostgreSQL required")
def test_real_postgres_uv():
    async def run():
        url = os.environ["AIGC_TEST_DATABASE_URL"]
        schema = "test_traffic_" + uuid.uuid4().hex
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 10, 19, 16, 1, tzinfo=UTC)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            with patch.object(service, "utc_now", return_value=now):
                async with sessions() as db:
                    empty = await service.get_traffic(db)
                    assert empty["tracking_since"] is None
                    assert len(empty["daily"]) == 30

                async def record():
                    async with sessions() as db:
                        await service.record_visit(db, "a" * 64)

                await asyncio.gather(*(record() for _ in range(8)))
                async with sessions() as db:
                    result = await service.get_traffic(db)
                    assert result["today_uv"] == result["seven_day_uv"] == 1
                    assert result["daily"][-1] == {"date": "2026-10-20", "uv": 1}
                    since = result["tracking_since"]
                    from app.models.visitor_daily import VisitorDaily

                    today = (now + timedelta(hours=8)).date()
                    db.add_all(
                        [
                            VisitorDaily(date=today - timedelta(days=6), visitor_hash="b" * 64),
                            VisitorDaily(date=today - timedelta(days=7), visitor_hash="c" * 64),
                            VisitorDaily(date=today - timedelta(days=29), visitor_hash="d" * 64),
                            VisitorDaily(date=today - timedelta(days=30), visitor_hash="e" * 64),
                            VisitorDaily(date=today + timedelta(days=1), visitor_hash="f" * 64),
                        ]
                    )
                    await db.commit()
                    windows = await service.get_traffic(db)
                    assert windows["today_uv"] == 1
                    assert windows["seven_day_uv"] == 2
                    assert windows["thirty_day_uv"] == 4

                with patch.object(service, "utc_now", return_value=now - timedelta(days=1)):
                    await record()
                async with sessions() as db:
                    result = await service.get_traffic(db)
                    assert result["seven_day_uv"] == 2
                    assert sum(row["uv"] for row in result["daily"]) == 5
                    await db.execute(
                        text("INSERT INTO visitor_daily (date, visitor_hash) VALUES (:day, :hash)"),
                        {"day": (now + timedelta(hours=8)).date() - timedelta(days=90), "hash": "b" * 64},
                    )
                    await db.commit()
                    assert await service.cleanup_visits(db) == 1
                    assert (await service.get_traffic(db))["tracking_since"] == since
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())


def test_routes_auth_privacy_and_body_limit(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers import traffic
    from app.utils import admin_auth

    monkeypatch.setattr(admin_auth, "get_settings", lambda: SimpleNamespace(admin_secret_key="test-admin"))
    app = FastAPI()
    app.include_router(traffic.router)

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    record = AsyncMock()
    monkeypatch.setattr(service, "record_visit", record)
    payload = {
        "generated_at": datetime.now(UTC),
        "tracking_since": None,
        "timezone": "Asia/Shanghai",
        "today_uv": 0,
        "seven_day_uv": 0,
        "thirty_day_uv": 0,
        "daily": [],
    }
    monkeypatch.setattr(service, "get_traffic", AsyncMock(return_value=payload))
    with TestClient(app) as client:
        for headers in ({}, {"X-Admin-Key": "wrong"}):
            assert client.get("/api/admin/traffic", headers=headers).status_code == 401
        response = client.get("/api/admin/traffic", headers={"X-Admin-Key": "test-admin"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "visitor_hash" not in response.text
        response = client.post("/api/analytics/visit", json={"visitor_hash": "a" * 64})
        assert response.status_code == 204 and response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert record.await_count == 1
        for body in (
            "{",
            '{"visitor_hash":"private-data"}',
            '{"visitor_hash":"' + "a" * 64 + '","ip":"private"}',
        ):
            response = client.post("/api/analytics/visit", content=body)
            assert response.status_code == 422
            assert "private" not in response.text
        assert client.post("/api/analytics/visit", content="a" * 257).status_code == 413
        assert client.post("/api/analytics/visit", content=(b"x" * 100 for _ in range(3))).status_code == 413
        assert record.await_count == 1
        assert "/api/analytics/visit" not in client.get("/openapi.json").json()["paths"]
