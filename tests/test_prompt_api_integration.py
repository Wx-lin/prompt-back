"""Private HTTP workflow against isolated PostgreSQL and namespaced real Redis.

Only the COS transport is replaced. No worker or paid model call is started.
"""

import asyncio
import io
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis_asyncio
from PIL import Image
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.database import Base, get_db
from app.main import app
from app.models import PromptAnalysis, PromptAnalysisAsset, User
from app.redis import get_redis_client
from app.services.prompt_analysis import storage
from app.utils.jwt import create_access_token

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis test URLs required",
    ),
]


class NamespacedRedis:
    def __init__(self, client):
        self.client = client
        self.prefix = "test_prompt_http:" + uuid.uuid4().hex + ":"
        self.keys = set()
        self.unavailable = False

    async def eval(self, script, numkeys, *arguments):
        if self.unavailable:
            raise RedisError("private redis transport failure")
        keys = [self.prefix + str(key) for key in arguments[:numkeys]]
        self.keys.update(keys)
        return await self.client.eval(script, numkeys, *keys, *arguments[numkeys:])


@pytest_asyncio.fixture
async def sessions():
    schema = "test_prompt_http_" + uuid.uuid4().hex
    url = os.environ["AIGC_TEST_DATABASE_URL"]
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def limiter():
    client = redis_asyncio.from_url(os.environ["AIGC_TEST_REDIS_URL"], decode_responses=True)
    await client.ping()
    wrapper = NamespacedRedis(client)
    try:
        yield wrapper
    finally:
        if wrapper.keys:
            await client.delete(*wrapper.keys)
        await client.aclose()


@pytest.fixture
def configured(monkeypatch):
    settings = get_settings()
    for key, value in {
        "prompt_analysis_enabled": True,
        "prompt_analysis_worker_enabled": False,
        "prompt_vision_model": "test-no-model-call",
        "prompt_vision_api_key": SecretStr("test-no-provider-call"),
        "prompt_analysis_daily_limit": 5,
        "prompt_analysis_global_daily_limit": 100,
        "prompt_analysis_queue_limit": 10,
    }.items():
        monkeypatch.setattr(settings, key, value)
    return settings


@pytest.fixture
def objects(monkeypatch):
    values = {}

    async def put(body, key):
        values[key] = body

    async def read(key):
        return values[key]

    async def delete(key):
        values.pop(key, None)

    def preview(key):
        assert key in values
        return "https://private-cos.example.test/preview?signature=" + uuid.uuid4().hex

    monkeypatch.setattr(storage, "require_storage_configuration", lambda: None)
    monkeypatch.setattr(storage, "put_private", put)
    monkeypatch.setattr(storage, "read_private", read)
    monkeypatch.setattr(storage, "delete_private", delete)
    monkeypatch.setattr(storage, "preview_url", preview)
    return values


@pytest_asyncio.fixture
async def api(sessions, limiter, configured, objects):
    async def database():
        async with sessions() as db:
            yield db

    async with sessions() as db:
        users = [User(nickname="HTTP owner"), User(nickname="Other owner")]
        db.add_all(users)
        await db.commit()
        headers = [{"Authorization": "Bearer " + create_access_token(user.id)} for user in users]
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_redis_client] = lambda: limiter
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield SimpleNamespace(client=client, owner=headers[0], other=headers[1], user_id=users[0].id)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def response_data(response, status=200):
    assert response.status_code == status, response.text
    assert "private" in response.headers["cache-control"]
    assert "no-store" in response.headers["cache-control"]
    if status == 204:
        assert not response.content
        return None
    payload = response.json()
    assert set(payload) == {"success", "data", "error", "request_id"}
    assert isinstance(payload["request_id"], str) and payload["request_id"]
    assert payload["success"] is (status < 400)
    if status < 400:
        assert payload["error"] is None
        return payload["data"]
    assert payload["data"] is None
    assert set(payload["error"]) == {"code", "message"}
    assert all(isinstance(payload["error"][key], str) for key in ("code", "message"))
    return payload["error"]


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (96, 64), "blue").save(output, format="PNG")
    return output.getvalue()


async def upload(api, headers=None):
    response = await api.client.post(
        "/api/prompt-assets", headers=headers or api.owner,
        files={"file": ("private.png", image_bytes(), "image/png")},
    )
    return response_data(response, 201)


async def create(api, asset_id, key=None, headers=None):
    return await api.client.post(
        "/api/prompt-analyses", json={"asset_id": asset_id},
        headers={**(headers or api.owner), "Idempotency-Key": key or str(uuid.uuid4())},
    )


def generated_result():
    return {
        "schema_version": "1", "title": "蓝色画面",
        "prompt": {"zh": "纯蓝色画面，均匀光线", "en": "Solid blue image, even lighting"},
        "breakdown": {
            key: {"zh": "", "en": ""}
            for key in ("subject", "composition", "lighting", "style")
        },
        "uncertain_details": [],
    }


async def finish(sessions, job_id):
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job

    claimed = await claim_job(sessions)
    assert claimed is not None and str(claimed.id) == job_id
    assert await dispatch_job(sessions, claimed.id, claimed.lease_token)
    assert await finish_job(sessions, claimed.id, claimed.lease_token, result=generated_result())


async def test_upload_create_result_edit_delete_and_tombstone(api, sessions, objects):
    asset = await upload(api)
    assert len(objects) == 2
    key = str(uuid.uuid4())
    created = response_data(await create(api, asset["id"], key), 202)
    job_id = created["id"]
    assert created["status"] == "queued"
    replay = response_data(await create(api, asset["id"], key))
    assert replay["id"] == job_id
    conflict = await create(api, asset["id"])
    assert response_data(conflict, 409)["code"] == "ANALYSIS_IN_PROGRESS"
    second_asset = await upload(api)
    assert response_data(await create(api, second_asset["id"], key), 409)["code"] == "IDEMPOTENCY_CONFLICT"
    running = response_data(await api.client.get("/api/prompt-analyses/" + job_id, headers=api.owner))
    assert running["status"] == "queued" and running["result"] is None
    await finish(sessions, job_id)
    detail = response_data(await api.client.get("/api/prompt-analyses/" + job_id, headers=api.owner))
    assert detail["status"] == "succeeded"
    assert detail["result"] == generated_result()
    assert detail["prompt"] == generated_result()["prompt"]
    edited = {"zh": "用户编辑的中文提示词", "en": "User edited English prompt"}
    payload = {"revision": detail["revision"], "prompt": edited}
    saved = response_data(await api.client.patch(
        "/api/prompt-analyses/" + job_id, headers=api.owner, json=payload,
    ))
    assert saved["prompt"] == edited and saved["revision"] == detail["revision"] + 1
    assert saved["result"] == generated_result()
    response_data(await api.client.patch(
        "/api/prompt-analyses/" + job_id, headers=api.owner, json=payload,
    ), 409)
    response_data(await api.client.delete("/api/prompt-analyses/" + job_id, headers=api.owner), 204)
    response_data(await api.client.get("/api/prompt-analyses/" + job_id, headers=api.owner), 404)
    assert response_data(await create(api, asset["id"], key), 404)["code"] == "NOT_FOUND"
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(job_id))
        assert stored.deleted_at is not None and stored.generated_result is None
        assert stored.edited_prompts is None
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 1


async def test_foreign_assets_and_records_are_hidden(api):
    asset = await upload(api)
    job = response_data(await create(api, asset["id"]), 202)
    response_data(await api.client.get("/api/prompt-assets/" + asset["id"], headers=api.other), 404)
    response_data(await create(api, asset["id"], headers=api.other), 404)
    path = "/api/prompt-analyses/" + job["id"]
    for method, kwargs in (
        ("get", {}), ("patch", {"json": {"revision": 0, "prompt": generated_result()["prompt"]}}),
        ("delete", {}),
    ):
        response_data(await getattr(api.client, method)(path, headers=api.other, **kwargs), 404)
    listing = response_data(await api.client.get("/api/prompt-analyses", headers=api.other))
    assert listing["items"] == []
    response_data(await api.client.get(path), 401)


async def test_same_key_concurrency_creates_one_row_and_reserves_one_quota(api, sessions, configured):
    configured.prompt_analysis_daily_limit = 1
    asset = await upload(api)
    key = str(uuid.uuid4())
    replies = await asyncio.gather(*(create(api, asset["id"], key) for _ in range(5)))
    assert sorted(reply.status_code for reply in replies) == [200, 200, 200, 200, 202]
    jobs = [response_data(reply, reply.status_code) for reply in replies]
    assert len({job["id"] for job in jobs}) == 1
    async with sessions() as db:
        rows = (await db.scalars(select(PromptAnalysis))).all()
        assert len(rows) == 1 and rows[0].quota_state == "reserved"
    await finish(sessions, jobs[0]["id"])
    another = await upload(api)
    assert response_data(await create(api, another["id"]), 429)["code"] == "QUOTA_EXCEEDED"


async def test_concurrent_owners_cannot_exceed_global_daily_quota(api, sessions, configured):
    configured.prompt_analysis_global_daily_limit = 1
    assets = [await upload(api), await upload(api, headers=api.other)]
    replies = await asyncio.gather(
        create(api, assets[0]["id"]), create(api, assets[1]["id"], headers=api.other),
    )
    assert sorted(reply.status_code for reply in replies) == [202, 429]
    for reply in replies:
        response_data(reply, reply.status_code)
        if reply.status_code == 429:
            assert reply.json()["error"]["code"] == "QUOTA_EXCEEDED"
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 1


async def test_redis_failure_blocks_new_work_but_keeps_read_edit_delete(api, sessions, limiter):
    asset = await upload(api)
    key = str(uuid.uuid4())
    job = response_data(await create(api, asset["id"], key), 202)
    await finish(sessions, job["id"])
    spare = await upload(api)
    limiter.unavailable = True
    replay = response_data(await create(api, asset["id"], key))
    assert replay["id"] == job["id"]
    failed_upload = await api.client.post(
        "/api/prompt-assets", headers=api.owner,
        files={"file": ("private.png", image_bytes(), "image/png")},
    )
    response_data(failed_upload, 503)
    failed_create = await create(api, spare["id"])
    response_data(failed_create, 503)
    assert "private redis transport" not in failed_create.text
    path = "/api/prompt-analyses/" + job["id"]
    detail = response_data(await api.client.get(path, headers=api.owner))
    response_data(await api.client.get("/api/prompt-analyses", headers=api.owner))
    response_data(await api.client.patch(
        path, headers=api.owner,
        json={"revision": detail["revision"], "prompt": {"zh": "离线编辑", "en": "Offline edit"}},
    ))
    response_data(await api.client.delete(path, headers=api.owner), 204)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 1
        assert await db.scalar(select(func.count()).select_from(PromptAnalysisAsset)) == 2


async def test_history_paginates_without_leaking_other_owner(api, sessions):
    own_ids = []
    for _ in range(3):
        asset = await upload(api)
        job = response_data(await create(api, asset["id"]), 202)
        own_ids.append(job["id"])
        await finish(sessions, job["id"])
    foreign_asset = await upload(api, headers=api.other)
    response_data(await create(api, foreign_asset["id"], headers=api.other), 202)
    first = response_data(await api.client.get(
        "/api/prompt-analyses?limit=2", headers=api.owner,
    ))
    assert first["pagination"]["limit"] == 2
    assert first["pagination"]["has_more"] is True
    cursor = first["pagination"]["next_cursor"]
    assert isinstance(cursor, str) and cursor
    second = response_data(await api.client.get(
        "/api/prompt-analyses", params={"limit": 2, "cursor": cursor}, headers=api.owner,
    ))
    assert second["pagination"]["has_more"] is False
    assert second["pagination"]["next_cursor"] is None
    assert [row["id"] for row in first["items"] + second["items"]] == list(reversed(own_ids))
    assert len(first["items"]) == 2 and len(second["items"]) == 1


async def test_owned_asset_refreshes_signed_previews_without_exposing_storage_keys(api, sessions):
    asset = await upload(api)
    refreshed = response_data(await api.client.get("/api/prompt-assets/" + asset["id"], headers=api.owner))
    assert refreshed["id"] == asset["id"]
    assert (refreshed["width"], refreshed["height"]) == (96, 64)
    assert refreshed["preview_url"] != asset["preview_url"]
    assert refreshed["thumbnail_url"] != asset["thumbnail_url"]
    assert refreshed["preview_expires_at"]
    async with sessions() as db:
        stored = await db.get(PromptAnalysisAsset, uuid.UUID(asset["id"]))
        assert stored.object_key not in str(refreshed)
        assert stored.thumbnail_key not in str(refreshed)


async def test_same_revision_concurrent_edits_have_one_winner(api, sessions):
    asset = await upload(api)
    job = response_data(await create(api, asset["id"]), 202)
    await finish(sessions, job["id"])
    path = "/api/prompt-analyses/" + job["id"]
    detail = response_data(await api.client.get(path, headers=api.owner))
    prompts = [{"zh": "第一版", "en": "First edit"}, {"zh": "第二版", "en": "Second edit"}]
    replies = await asyncio.gather(*(
        api.client.patch(path, headers=api.owner, json={"revision": detail["revision"], "prompt": prompt})
        for prompt in prompts
    ))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    winner = next(index for index, reply in enumerate(replies) if reply.status_code == 200)
    loser = response_data(replies[1 - winner], 409)
    assert loser["code"] == "REVISION_CONFLICT"
    saved = response_data(await api.client.get(path, headers=api.owner))
    assert saved["prompt"] == prompts[winner]
    assert saved["revision"] == detail["revision"] + 1
    assert saved["result"] == generated_result()


async def test_deleting_queued_job_releases_quota_and_never_replays_it(api, sessions, configured):
    configured.prompt_analysis_daily_limit = 1
    asset = await upload(api)
    key = str(uuid.uuid4())
    job = response_data(await create(api, asset["id"], key), 202)
    response_data(await api.client.delete("/api/prompt-analyses/" + job["id"], headers=api.owner), 204)
    response_data(await create(api, asset["id"], key), 404)
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(job["id"]))
        assert stored.status == "canceled" and stored.quota_state == "released"
        assert stored.deleted_at is not None
    replacement = await upload(api)
    response_data(await create(api, replacement["id"]), 202)


@pytest.mark.parametrize("failure_stage", ["second_put", "preview"])
async def test_failed_upload_compensates_both_objects_and_keeps_cleanup_row(
    api, sessions, objects, monkeypatch, failure_stage,
):
    put_private = storage.put_private
    delete_private = storage.delete_private
    writes, deletions = [], []

    async def put(body, key):
        writes.append(key)
        await put_private(body, key)
        if failure_stage == "second_put" and len(writes) == 2:
            raise OSError("private storage failure")

    async def delete(key):
        deletions.append(key)
        await delete_private(key)

    def preview(key):
        raise OSError("private preview signing failure")

    monkeypatch.setattr(storage, "put_private", put)
    monkeypatch.setattr(storage, "delete_private", delete)
    if failure_stage == "preview":
        monkeypatch.setattr(storage, "preview_url", preview)
    response = await api.client.post(
        "/api/prompt-assets", headers=api.owner,
        files={"file": ("private.png", image_bytes(), "image/png")},
    )
    assert response_data(response, 503)["code"] == "STORAGE_UNAVAILABLE"
    assert "private storage" not in response.text and "private preview" not in response.text
    async with sessions() as db:
        rows = (await db.scalars(select(PromptAnalysisAsset))).all()
        assert len(rows) == 1 and rows[0].state == "uploading"
        assert set(deletions) == {rows[0].object_key, rows[0].thumbnail_key}
        assert len(deletions) == 2 and len(writes) == 2
    assert objects == {}


async def test_delete_observes_worker_dispatch_between_initial_read_and_row_lock(api, sessions, monkeypatch):
    from app.services.prompt_analysis import api as service
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job

    asset = await upload(api)
    job = response_data(await create(api, asset["id"]), 202)
    job_id = uuid.UUID(job["id"])
    original_lock = service.budget_lock
    claims = []

    async def claim_before_lock(db):
        claim = await claim_job(sessions)
        assert claim is not None and claim.id == job_id
        assert await dispatch_job(sessions, job_id, claim.lease_token)
        claims.append(claim)
        await original_lock(db)

    monkeypatch.setattr(service, "budget_lock", claim_before_lock)
    path = "/api/prompt-analyses/" + job["id"]
    response_data(await api.client.delete(path, headers=api.owner), 204)
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job_id)
        assert stored.deleted_at is not None
        assert stored.status == "running" and stored.quota_state == "reserved"
        assert stored.lease_token == claims[0].lease_token and stored.finished_at is None
    assert await finish_job(sessions, job_id, claims[0].lease_token, result=generated_result())
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job_id)
        assert stored.status == "canceled" and stored.quota_state == "consumed"
        assert stored.generated_result is None and stored.edited_prompts is None
    response_data(await api.client.get(path, headers=api.owner), 404)


async def test_ready_commit_failure_rolls_back_then_compensates_without_expired_orm_access(
    api, sessions, objects, monkeypatch,
):
    deletions, commits = [], []
    delete_private = storage.delete_private

    async def database():
        async with sessions() as db:
            original_commit = db.commit

            async def commit():
                commits.append(True)
                if len(commits) == 2:
                    await db.flush()
                    raise OperationalError("private asset update", {}, OSError("private database failure"))
                await original_commit()

            monkeypatch.setattr(db, "commit", commit)
            yield db

    async def delete(key):
        deletions.append(key)
        await delete_private(key)

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(storage, "delete_private", delete)
    response = await api.client.post(
        "/api/prompt-assets", headers=api.owner,
        files={"file": ("private.png", image_bytes(), "image/png")},
    )
    assert response_data(response, 503)["code"] == "STORAGE_UNAVAILABLE"
    assert "private database" not in response.text and len(commits) == 2
    async with sessions() as db:
        rows = (await db.scalars(select(PromptAnalysisAsset))).all()
        assert len(rows) == 1 and rows[0].state == "uploading"
        assert set(deletions) == {rows[0].object_key, rows[0].thumbnail_key}
        assert len(deletions) == 2
    assert objects == {}
