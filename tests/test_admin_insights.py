"""Admin insights authentication and query validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import admin_insights
from app.utils import admin_auth


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(admin_auth, "get_settings", lambda: SimpleNamespace(admin_secret_key="insights-test"))
    app = FastAPI()
    app.include_router(admin_insights.router)

    async def fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = fake_db
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", ["dashboard", "users"])
@pytest.mark.parametrize("headers", [{}, {"X-Admin-Key": "wrong"}])
def test_admin_required(client, path, headers):
    assert client.get(f"/api/admin/{path}", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "query", ["page=0", "page_size=0", "page_size=101", "is_active=invalid", "search=" + "x" * 201]
)
def test_query_validation(client, query):
    assert (
        client.get("/api/admin/users?" + query, headers={"X-Admin-Key": "insights-test"}).status_code == 422
    )


def test_successful_responses_disable_caching(client, monkeypatch):
    payload = dict(items=[], total=0, all_users_total=0, page=1, page_size=20)
    list_mock = AsyncMock(return_value=payload)
    monkeypatch.setattr(admin_insights.admin_insights_service, "list_users", list_mock)
    response = client.get(
        "/api/admin/users?search=%20name%20&is_active=false", headers={"X-Admin-Key": "insights-test"}
    )
    assert response.status_code == 200 and response.json() == payload
    assert response.headers.get("cache-control") == "no-store"
    assert list_mock.call_args.args[1:] == (" name ", False, 1, 20)
    dashboard = dict(
        generated_at="2026-10-16T00:00:00Z",
        users=dict(total=0, enabled=0, disabled=0, today_new=0),
        content=dict(total=0, published=0, pending=0, rejected=0, other_unpublished=0, images=0),
        crawl=dict(total=0, pending=0),
        categories=[],
    )
    monkeypatch.setattr(
        admin_insights.admin_insights_service, "get_dashboard", AsyncMock(return_value=dashboard)
    )
    response = client.get("/api/admin/dashboard", headers={"X-Admin-Key": "insights-test"})
    assert response.status_code == 200 and response.json() == dashboard
    assert response.headers.get("cache-control") == "no-store"
