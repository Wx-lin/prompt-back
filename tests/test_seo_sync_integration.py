"""Real isolated PostgreSQL synchronization preserves the last good source."""

import asyncio
import os
import uuid
from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.seo import SeoSnapshot
from app.models.seo_sync import SeoApiDay, SeoSyncState
from app.services.google_search_console import GscError
from app.services.seo_sync_service import run_sync, sync_status

pytestmark = pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit isolated DB required"
)


class FakeProvider:
    fail = False

    async def fetch_day(self, day, dimension):
        if self.fail:
            raise GscError("permission_denied")
        if dimension == "query":
            return [], False
        return [
            {
                "key": day.isoformat() if dimension == "daily" else "https://aigc-prompt.com/zh",
                "clicks": 1,
                "impressions": 10,
                "position": 3,
            }
        ], False


def test_atomic_idempotent_and_failure_preserves_success(monkeypatch):
    async def run():
        url = os.environ["AIGC_TEST_DATABASE_URL"]
        schema = "test_seo_sync_" + uuid.uuid4().hex
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with admin.begin() as db:
                await db.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as db:
                for table in [SeoSnapshot.__table__, SeoApiDay.__table__, SeoSyncState.__table__]:
                    await db.run_sync(table.create)
            provider = FakeProvider()
            end = date(2026, 10, 20)
            assert await run_sync(sessions, provider, end=end, days=2) == "success"
            assert await run_sync(sessions, provider, end=end, days=2) == "success"
            async with sessions() as db:
                assert len((await db.scalars(select(SeoApiDay))).all()) == 6
                db.add(
                    SeoSnapshot(
                        engine="google",
                        dimension="daily",
                        start_date=end,
                        end_date=end,
                        rows=[{"key": end.isoformat(), "clicks": 999, "impressions": 1000}],
                    )
                )
                await db.commit()
                from app.services.seo_service import get_report

                report = await get_report(db, "google", end, end)
                assert report["connection_status"] == "connected"
                assert report["totals"]["clicks"] == 1
                assert report["queries"] == []
                assert (await sync_status(db, configured=True))["last_success_at"]
                before = (await sync_status(db, configured=True))["last_success_at"]
            provider.fail = True
            assert await run_sync(sessions, provider, end=end, days=2) == "failed"
            async with sessions() as db:
                state = await sync_status(db, configured=True)
                assert state["status"] == "failed" and state["last_error"] == "permission_denied"
                assert state["last_success_at"] == before
                assert len((await db.scalars(select(SeoApiDay))).all()) == 6
                assert not (await sync_status(db, configured=False))["configured"]
            # Concurrent sync is rejected without replacing the durable running attempt.
            started, release = asyncio.Event(), asyncio.Event()

            class BlockingProvider(FakeProvider):
                async def fetch_day(self, day, dimension):
                    started.set()
                    await release.wait()
                    return await super().fetch_day(day, dimension)

            active = asyncio.create_task(run_sync(sessions, BlockingProvider(), end=end, days=1))
            await started.wait()
            assert await run_sync(sessions, FakeProvider(), end=end, days=1) == "busy"
            async with sessions() as db:
                assert (await sync_status(db, configured=True))["status"] == "running"
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            async with sessions() as db:
                assert (await sync_status(db, configured=True))["last_error"] == "interrupted"

            # An upstream success containing no rows stays missing, never fabricated zero.
            class EmptyProvider:
                async def fetch_day(self, day, dimension):
                    return [], False

            assert await run_sync(sessions, EmptyProvider(), end=end, days=1) == "success"
            async with sessions() as db:
                report = await get_report(db, "google", end, end)
                assert report["totals"] is None and report["daily"] == []
                assert report["coverage"]["daily"]["status"] == "partial"
            # External runner bundle is validated and passed through the same locked atomic writer.
            from types import SimpleNamespace

            from app.services import seo_bundle_service, seo_sync_service

            external_settings = SimpleNamespace(
                gsc_enabled=True, gsc_sync_mode="external", gsc_credentials_file=""
            )
            monkeypatch.setattr(seo_bundle_service, "get_settings", lambda: external_settings)
            monkeypatch.setattr(seo_sync_service, "get_settings", lambda: external_settings)
            raw = await seo_bundle_service.export_bundle(FakeProvider(), previous=end, target=end)
            assert await seo_bundle_service.import_bundle(sessions, raw) == "success"
            assert await seo_bundle_service.import_bundle(sessions, raw) == "success"
            async with sessions() as db:
                report = await get_report(db, "google", end, end)
                assert report["totals"]["clicks"] == 1
                assert report["sync"]["mode"] == "external"
                assert not report["sync"]["trigger_available"]
                success = report["sync"]["last_success_at"]
            stale = await seo_bundle_service.export_bundle(
                FakeProvider(), previous=None, target=date(2026, 9, 1)
            )
            assert await seo_bundle_service.import_bundle(sessions, stale) == "failed"
            async with sessions() as db:
                state = await sync_status(db)
                assert state["last_error"] == "stale_bundle" and state["last_success_at"] == success
                assert (await get_report(db, "google", end, end))["totals"]["clicks"] == 1
            from datetime import UTC, datetime, timedelta

            assert not await seo_bundle_service.record_external_failure(
                sessions, success - timedelta(seconds=1), "upstream_unavailable"
            )
            assert await seo_bundle_service.record_external_failure(
                sessions, datetime.now(UTC), "upstream_unavailable"
            )
            async with sessions() as db:
                state = await sync_status(db)
                assert state["status"] == "failed" and state["last_error"] == "upstream_unavailable"
                assert state["last_success_at"] == success
                assert (await get_report(db, "google", end, end))["totals"]["clicks"] == 1
        finally:
            await engine.dispose()
            async with admin.begin() as db:
                await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(run())
