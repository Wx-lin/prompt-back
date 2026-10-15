"""Admin and submission pages share the canonical category catalog through their own routes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import admin, categories
from app.utils import admin_auth


def test_admin_catalog_matches_public_catalog_and_requires_auth(monkeypatch):
    monkeypatch.setattr(admin_auth, 'get_settings', lambda: SimpleNamespace(admin_secret_key='test-only'))
    db = AsyncMock()
    db.execute.return_value = Mock(all=lambda: [('portrait', 2)])
    app = FastAPI()
    app.include_router(admin.router)
    app.include_router(categories.router)

    async def fake_db():
        yield db

    app.dependency_overrides[get_db] = fake_db
    with TestClient(app) as client:
        assert client.get('/api/admin/categories').status_code == 401
        public = client.get('/api/categories')
        response = client.get('/api/admin/categories', headers={'X-Admin-Key': 'test-only'})
    assert response.status_code == 200
    assert response.json() == public.json()
    assert len(response.json()) == 12
    assert response.json()[0]['key'] == 'portrait'
    assert response.json()[0]['label'] == '人像写真'
    assert response.json()[0]['count'] == 2
