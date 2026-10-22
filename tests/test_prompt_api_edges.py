"""Business boundaries through the real app, PostgreSQL and Redis fixtures."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.models import PromptAnalysis, PromptAnalysisAsset
from tests.test_prompt_api_integration import (
    api as api,
)
from tests.test_prompt_api_integration import (
    configured as configured,
)
from tests.test_prompt_api_integration import (
    create,
    finish,
    generated_result,
    image_bytes,
    response_data,
    upload,
)
from tests.test_prompt_api_integration import (
    limiter as limiter,
)
from tests.test_prompt_api_integration import (
    objects as objects,
)
from tests.test_prompt_api_integration import pytestmark as integration_marks
from tests.test_prompt_api_integration import (
    sessions as sessions,
)

pytestmark = integration_marks


@pytest.mark.parametrize("key", [None, "", "not-a-uuid", "a" * 129])
async def test_missing_or_malformed_idempotency_key_does_not_create_job(api, sessions, key):
    asset = await upload(api)
    headers = dict(api.owner)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = await api.client.post(
        "/api/prompt-analyses", headers=headers, json={"asset_id": asset["id"]},
    )
    response_data(response, 422)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 0


async def test_idempotency_key_cannot_change_the_asset(api, sessions):
    first, second = await upload(api), await upload(api)
    key = str(uuid.uuid4())
    original = response_data(await create(api, first["id"], key), 202)
    error = response_data(await create(api, second["id"], key), 409)
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    async with sessions() as db:
        rows = (await db.scalars(select(PromptAnalysis))).all()
        assert len(rows) == 1 and str(rows[0].id) == original["id"]
        assert str(rows[0].asset_id) == first["id"]


async def test_disabled_feature_keeps_history_edit_delete_and_replay(api, sessions, configured):
    asset = await upload(api)
    spare = await upload(api)
    key = str(uuid.uuid4())
    job = response_data(await create(api, asset["id"], key), 202)
    await finish(sessions, job["id"])
    configured.prompt_analysis_enabled = False
    path = "/api/prompt-analyses/" + job["id"]
    detail = response_data(await api.client.get(path, headers=api.owner))
    assert detail["result"] == generated_result()
    history = response_data(await api.client.get("/api/prompt-analyses", headers=api.owner))
    assert [row["id"] for row in history["items"]] == [job["id"]]
    response_data(await api.client.get("/api/prompt-assets/" + asset["id"], headers=api.owner))
    assert response_data(await create(api, asset["id"], key))["id"] == job["id"]
    error = response_data(await create(api, spare["id"]), 503)
    assert error["code"] == "FEATURE_UNAVAILABLE"
    error = response_data(await api.client.post(
        "/api/prompt-assets", headers=api.owner,
        files={"file": ("private.png", image_bytes(), "image/png")},
    ), 503)
    assert error["code"] == "FEATURE_UNAVAILABLE"
    edited = {"zh": "关闭功能后仍能编辑", "en": "Editable while the feature is disabled"}
    saved = response_data(await api.client.patch(
        path, headers=api.owner, json={"revision": detail["revision"], "prompt": edited},
    ))
    assert saved["prompt"] == edited
    response_data(await api.client.delete(path, headers=api.owner), 204)


async def test_missing_provider_key_rejects_creation_without_reserving_quota(api, sessions, configured):
    asset = await upload(api)
    configured.prompt_vision_api_key = SecretStr("")
    error = response_data(await create(api, asset["id"]), 503)
    assert error["code"] == "MODEL_UNAVAILABLE"
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 0
        assert await db.scalar(select(func.count()).select_from(PromptAnalysisAsset)) == 1


async def test_completed_job_still_consumes_personal_daily_quota(api, sessions, configured):
    configured.prompt_analysis_daily_limit = 1
    first = await upload(api)
    job = response_data(await create(api, first["id"]), 202)
    await finish(sessions, job["id"])
    second = await upload(api)
    assert response_data(await create(api, second["id"]), 429)["code"] == "QUOTA_EXCEEDED"
    async with sessions() as db:
        rows = (await db.scalars(select(PromptAnalysis))).all()
        assert len(rows) == 1 and rows[0].status == "succeeded"
        assert rows[0].quota_state == "consumed"


async def test_full_global_queue_rejects_other_user_without_reserving_quota(api, sessions, configured):
    configured.prompt_analysis_queue_limit = 1
    first = await upload(api)
    second = await upload(api, headers=api.other)
    job = response_data(await create(api, first["id"]), 202)
    error = response_data(await create(api, second["id"], headers=api.other), 503)
    assert error["code"] == "QUEUE_FULL"
    async with sessions() as db:
        rows = (await db.scalars(select(PromptAnalysis))).all()
        assert len(rows) == 1 and str(rows[0].id) == job["id"]
        assert rows[0].status == "queued" and rows[0].quota_state == "reserved"


async def test_expired_unbound_asset_is_hidden_and_cannot_create_job(api, sessions):
    asset = await upload(api)
    async with sessions() as db:
        stored = await db.get(PromptAnalysisAsset, uuid.UUID(asset["id"]))
        stored.unbound_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    error = response_data(await api.client.get("/api/prompt-assets/" + asset["id"], headers=api.owner), 404)
    assert error["code"] == "NOT_FOUND"
    assert response_data(await create(api, asset["id"]), 404)["code"] == "NOT_FOUND"
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 0


async def test_history_expiry_hides_get_and_idempotent_replay_before_cleanup(api, sessions):
    asset = await upload(api)
    key = str(uuid.uuid4())
    job = response_data(await create(api, asset["id"], key), 202)
    await finish(sessions, job["id"])
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(job["id"]))
        stored.history_retain_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    path = "/api/prompt-analyses/" + job["id"]
    assert response_data(await api.client.get(path, headers=api.owner), 404)["code"] == "NOT_FOUND"
    assert response_data(await create(api, asset["id"], key), 404)["code"] == "NOT_FOUND"
    history = response_data(await api.client.get("/api/prompt-analyses", headers=api.owner))
    assert history["items"] == []
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, uuid.UUID(job["id"]))
        assert stored.deleted_at is None and stored.generated_result == generated_result()
        assert await db.scalar(select(func.count()).select_from(PromptAnalysis)) == 1


@pytest.mark.parametrize("cursor", ["not-base64", "e30="])
async def test_invalid_history_cursor_is_a_private_validation_error(api, cursor):
    error = response_data(await api.client.get(
        "/api/prompt-analyses", headers=api.owner, params={"cursor": cursor},
    ), 422)
    assert error["code"] == "INVALID_CURSOR"


@pytest.mark.parametrize("status", ["queued", "running", "failed", "unknown"])
async def test_non_successful_job_cannot_be_edited(api, sessions, status):
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job

    asset = await upload(api)
    job = response_data(await create(api, asset["id"]), 202)
    job_id = uuid.UUID(job["id"])
    if status != "queued":
        claim = await claim_job(sessions)
        assert claim is not None and claim.id == job_id
        if status in {"failed", "unknown"}:
            assert await dispatch_job(sessions, job_id, claim.lease_token)
            assert await finish_job(
                sessions, job_id, claim.lease_token,
                error_code="provider_error", uncertain=status == "unknown",
            )
    error = response_data(await api.client.patch(
        "/api/prompt-analyses/" + job["id"], headers=api.owner,
        json={"revision": 0, "prompt": generated_result()["prompt"]},
    ), 409)
    assert error["code"] == "NOT_EDITABLE"
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job_id)
        assert stored.status == status and stored.revision == 0
        assert stored.generated_result is None and stored.edited_prompts is None


@pytest.mark.parametrize(
    "setting", ["prompt_analysis_upload_hourly_limit", "prompt_analysis_global_upload_daily_limit"],
)
async def test_upload_rate_limit_rejects_before_creating_assets_or_storing_objects(
    api, sessions, configured, objects, monkeypatch, setting,
):
    monkeypatch.setattr(configured, setting, 1)
    await upload(api)
    original_objects = dict(objects)
    error = response_data(await api.client.post(
        "/api/prompt-assets", headers=api.owner,
        files={"file": ("private.png", image_bytes(), "image/png")},
    ), 429)
    assert error["code"] == "RATE_LIMITED"
    assert objects == original_objects and len(objects) == 2
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PromptAnalysisAsset)) == 1
