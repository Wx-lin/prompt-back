"""Authentication, durable preparation before response, and scheduling opt-in."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.seo import router
from app.services import seo_sync_service
from app.utils.admin_auth import verify_admin_key


def test_sync_auth_off_and_claimed_before_task(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post("/api/admin/seo/sync/google").status_code == 401
    app.dependency_overrides[verify_admin_key] = lambda: None
    monkeypatch.setattr(seo_sync_service, "configured", lambda: False)
    assert client.post("/api/admin/seo/sync/google").status_code == 409
    monkeypatch.setattr(seo_sync_service, "configured", lambda: True)
    monkeypatch.setattr(seo_sync_service, "prepare_sync", AsyncMock(return_value=None))
    assert client.post("/api/admin/seo/sync/google").status_code == 409
    attempt = datetime.now(UTC)
    prepare = AsyncMock(return_value=attempt)
    run = AsyncMock()
    monkeypatch.setattr(seo_sync_service, "prepare_sync", prepare)
    monkeypatch.setattr(seo_sync_service, "run_configured_sync", run)
    response = client.post("/api/admin/seo/sync/google")
    assert response.status_code == 202 and response.json() == {"accepted": True}
    assert response.headers["cache-control"] == "no-store"
    prepare.assert_awaited_once()
    run.assert_awaited_once_with(prepared_at=attempt)
    monkeypatch.setattr(seo_sync_service, "sync_status", AsyncMock(return_value={"status": "running"}))
    # Override DB dependency so a status-only request never touches production.
    from app.database import get_db

    app.dependency_overrides[get_db] = lambda: None
    assert client.get("/api/admin/seo/sync/google/status").json() == {"status": "running"}


def test_scheduler_opt_in(monkeypatch):
    scheduler = MagicMock()
    monkeypatch.setattr(seo_sync_service, "configured", lambda: False)
    seo_sync_service.register_seo_job(scheduler)
    scheduler.add_job.assert_not_called()
    monkeypatch.setattr(seo_sync_service, "configured", lambda: True)
    seo_sync_service.register_seo_job(scheduler)
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["timezone"] == "America/Los_Angeles" and kwargs["max_instances"] == 1


def test_sync_window_catches_up_in_bounded_contiguous_batches():
    from datetime import date

    from app.services.seo_sync_service import sync_window

    # A month-long outage must advance from the prior frontier, not skip to last week.
    end, days = sync_window(date(2026, 9, 1), date(2026, 10, 20))
    assert end == date(2026, 9, 22) and days == 28
    end, days = sync_window(date(2026, 10, 18), date(2026, 10, 20))
    assert end == date(2026, 10, 20) and days == 9


def test_configured_factory_uses_dedicated_credentials(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(
        seo_sync_service,
        "get_settings",
        lambda: SimpleNamespace(gsc_enabled=False, gsc_credentials_file="", gsc_sync_mode="server"),
    )
    assert not seo_sync_service.configured()
    assert asyncio.run(seo_sync_service.run_configured_sync()) == "not_configured"
    monkeypatch.setattr(
        seo_sync_service,
        "get_settings",
        lambda: SimpleNamespace(
            gsc_enabled=True, gsc_credentials_file="/private/read-only.json", gsc_sync_mode="server"
        ),
    )
    run = AsyncMock(return_value="success")
    monkeypatch.setattr(seo_sync_service, "run_sync", run)
    assert asyncio.run(seo_sync_service.run_configured_sync()) == "success"
    provider = run.call_args.args[1]
    assert provider.token.filename == "/private/read-only.json"


def test_external_mode_never_triggers_server_network(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    settings = SimpleNamespace(gsc_enabled=True, gsc_credentials_file="", gsc_sync_mode="external")
    monkeypatch.setattr(seo_sync_service, "get_settings", lambda: settings)
    assert seo_sync_service.configured()
    scheduler = MagicMock()
    seo_sync_service.register_seo_job(scheduler)
    scheduler.add_job.assert_not_called()
    assert asyncio.run(seo_sync_service.run_configured_sync()) == "not_configured"
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_admin_key] = lambda: None
    prepare = AsyncMock()
    monkeypatch.setattr(seo_sync_service, "prepare_sync", prepare)
    response = TestClient(app).post("/api/admin/seo/sync/google")
    assert response.status_code == 409 and "GitHub Actions" in response.json()["detail"]
    prepare.assert_not_awaited()
