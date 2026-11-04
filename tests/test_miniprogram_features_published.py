"""The creation switch is durable, private to administrators and fail-closed."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_db
from app.models.miniprogram_features import MiniProgramFeatures
from app.redis import get_redis_client
from app.routers import prompt_analyses
from app.routers.miniprogram_features import router
from app.routers.prompt_analyses import router as analysis_router
from app.utils.admin_auth import verify_admin_key


class FakeRedis:
    async def eval(self, *args):
        return 1


def test_switch_defaults_closed_and_persists_across_requests(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'features.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(MiniProgramFeatures.__table__.create)
            await conn.execute(MiniProgramFeatures.__table__.insert().values(id=1, creation_enabled=False))

    asyncio.run(setup())

    async def db_override():
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(router)
    app.include_router(analysis_router)
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_redis_client] = lambda: FakeRedis()
    monkeypatch.setattr(prompt_analyses, "get_redis_client", lambda: FakeRedis())
    client = TestClient(app)
    public = "/api/miniprogram/features"
    admin = "/api/admin/miniprogram/features"
    response = client.get(public)
    assert response.json() == {"creation_enabled": False}
    assert response.headers["cache-control"] == "no-store"
    old = client.get("/api/prompt-examples")
    assert old.json()["data"] == {"items": []}
    assert old.headers["cache-control"] == "public, max-age=300"
    examples = client.get("/api/prompt-examples?features=1")
    assert examples.json()["data"] == {"items": [], "creation_enabled": False}
    assert examples.headers["cache-control"] == "no-store"
    assert client.get(admin).status_code == 401
    assert client.put(admin, json={"creation_enabled": True}).status_code == 401
    app.dependency_overrides[verify_admin_key] = lambda: None
    assert client.put(admin, json={"creation_enabled": "true"}).status_code == 422
    assert client.put(admin, json={"creation_enabled": True, "other": 1}).status_code == 422
    assert client.put(admin, json={"creation_enabled": True}).json() == {"creation_enabled": True}
    assert client.get(public).json() == {"creation_enabled": True}
    assert client.get("/api/prompt-examples?features=1").json()["data"]["creation_enabled"] is True
    assert client.get(admin).json() == {"creation_enabled": True}
    assert client.put(admin, json={"creation_enabled": False}).json() == {"creation_enabled": False}
    assert client.get(public).json() == {"creation_enabled": False}
    asyncio.run(engine.dispose())


def test_migration_seeds_closed_switch_and_downgrades():
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[1] / "alembic/versions/c2b8e0364a21_miniprogram_features.py"
    spec = importlib.util.spec_from_file_location("mini_features_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            migration.upgrade()
            assert conn.execute(sa.text("select id, creation_enabled from miniprogram_features")).all() == [
                (1, 0)
            ]
            migration.downgrade()
        assert not sa.inspect(conn).has_table("miniprogram_features")




def test_feature_endpoints_rate_limit_and_fail_closed(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(MiniProgramFeatures.__table__.create)
            await conn.execute(MiniProgramFeatures.__table__.insert().values(id=1, creation_enabled=False))

    asyncio.run(setup())

    async def db_override():
        async with sessions() as session:
            yield session

    class RedisCounter:
        def __init__(self):
            self.counts = {}
            self.fail = False

        async def eval(self, script, nkeys, key, seconds):
            if self.fail:
                raise RuntimeError("redis unavailable")
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

    redis = RedisCounter()
    app = FastAPI()
    app.include_router(router)
    app.include_router(analysis_router)
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_redis_client] = lambda: redis
    app.dependency_overrides[verify_admin_key] = lambda: None
    monkeypatch.setattr(prompt_analyses, "get_redis_client", lambda: redis)
    client = TestClient(app)
    for endpoint in (
        "/api/miniprogram/features", "/api/prompt-examples?features=1", "/api/admin/miniprogram/features"
    ):
        for _ in range(125):
            result = client.get(endpoint)
            if result.status_code == 429:
                break
        assert result.status_code == 429, endpoint
    assert all("testclient" not in key for key in redis.counts)
    redis.fail = True
    for endpoint in (
        "/api/miniprogram/features", "/api/prompt-examples?features=1", "/api/admin/miniprogram/features"
    ):
        assert client.get(endpoint).status_code == 503
    assert client.put("/api/admin/miniprogram/features", json={"creation_enabled": True}).status_code == 503
    assert client.get("/api/prompt-examples").status_code == 200
    asyncio.run(engine.dispose())


def test_rate_limit_applies_before_admin_authentication():
    class LimitedRedis:
        async def eval(self, *args):
            return 31

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_redis_client] = lambda: LimitedRedis()
    response = TestClient(app).get("/api/admin/miniprogram/features")
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
