"""Real PostgreSQL snapshot replacement and aggregation contract."""

import asyncio
import os
import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.seo import SeoSnapshot
from app.models.seo_sync import SeoApiDay, SeoSyncState
from app.schemas.seo import SeoImport
from app.services.seo_service import get_report, import_snapshot

pytestmark = pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit isolated test DB required"
)


def test_snapshot_windows_and_totals():
    async def run():
        url = os.environ["AIGC_TEST_DATABASE_URL"]
        schema = "test_seo_" + uuid.uuid4().hex
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                for table in [SeoSnapshot.__table__, SeoApiDay.__table__, SeoSyncState.__table__]:
                    await conn.run_sync(table.create)
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                start, end = date(2026, 10, 1), date(2026, 10, 2)
                assert (await get_report(db, "google", start, end))["totals"] is None
                base = dict(engine="google", dimension="daily", start_date=start, end_date=end)
                rows = [{"key": "2026-10-01", "clicks": 2, "impressions": 10, "position": 3}]
                await import_snapshot(db, SeoImport(**base, rows=rows))
                await import_snapshot(db, SeoImport(**base, rows=rows))
                report = await get_report(db, "google", start, end)
                assert report["totals"]["clicks"] == 2
                assert report["coverage"]["daily"]["status"] == "partial"
                assert len(report["daily"]) == 1
                await import_snapshot(
                    db,
                    SeoImport(
                        **{**base, "dimension": "query"},
                        rows=[{"key": "ai 提示词", "clicks": 99, "impressions": 100}],
                    ),
                )
                report = await get_report(db, "google", start, end)
                assert report["totals"]["clicks"] == 2
                assert report["queries"][0]["clicks"] == 99
                assert report["coverage"]["query"]["status"] == "complete"
                with pytest.raises(HTTPException) as error:
                    await import_snapshot(db, SeoImport(**{**base, "end_date": date(2026, 10, 3)}, rows=rows))
                assert error.value.status_code == 409
                await db.rollback()
                assert (await get_report(db, "baidu", start, end))["connection_status"] == "not_connected"
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())
