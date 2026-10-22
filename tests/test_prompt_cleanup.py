"""Expiry and private-object cleanup preserve all live references and leases."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from tests.test_prompt_jobs import seed_job, valid_result
from tests.test_prompt_jobs import sessions as _sessions

sessions = _sessions
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit test DB required"),
]


async def test_cleanup_honors_max_retention_and_active_lease(sessions, monkeypatch):
    from app.models import PromptAnalysis, PromptAnalysisAsset
    from app.services.prompt_analysis import cleanup
    from app.services.prompt_analysis.jobs import claim_job

    now = datetime.now(UTC)
    original, asset = await seed_job(sessions, image_retain_until=now - timedelta(hours=1))
    async with sessions() as db:
        db.add(
            PromptAnalysis(
                user_id=original.user_id,
                asset_id=asset.id,
                idempotency_key="another",
                request_hash="2" * 64,
                source_client="web",
                prompt_version="v1",
                provider="test",
                model="test",
                config_snapshot={},
                quota_date=now.date(),
                history_retain_until=now + timedelta(days=30),
                image_retain_until=now + timedelta(days=20),
            )
        )
        await db.commit()
    delete = AsyncMock()
    monkeypatch.setattr(cleanup.storage, "delete_private", delete)
    assert await cleanup.cleanup_assets(sessions) == 0
    delete.assert_not_awaited()
    claim = await claim_job(sessions)
    async with sessions() as db:
        for row in (await db.scalars(__import__("sqlalchemy").select(PromptAnalysis))).all():
            row.deleted_at = now
            row.image_retain_until = now - timedelta(hours=1)
        await db.commit()
    assert await cleanup.cleanup_assets(sessions) == 0
    async with sessions() as db:
        row = await db.get(PromptAnalysis, claim.id)
        row.lease_expires_at = now - timedelta(seconds=1)
        await db.commit()
    assert await cleanup.cleanup_assets(sessions) == 1
    assert delete.await_count == 2
    async with sessions() as db:
        row = await db.get(PromptAnalysisAsset, asset.id)
        assert row.state == "deleted"
    assert await cleanup.cleanup_assets(sessions) == 0


async def test_partial_upload_and_failed_cleanup_are_retryable(sessions, monkeypatch):
    from app.models import PromptAnalysisAsset, User
    from app.services.prompt_analysis import cleanup

    now = datetime.now(UTC)
    async with sessions() as db:
        user = User(nickname="partial upload")
        db.add(user)
        await db.flush()
        asset = PromptAnalysisAsset(
            user_id=user.id,
            object_key="private/a.jpg",
            thumbnail_key="private/t.jpg",
            sha256="0" * 64,
            width=1,
            height=1,
            byte_size=1,
            state="uploading",
            unbound_expires_at=now - timedelta(hours=1),
        )
        db.add(asset)
        await db.commit()
        asset_id = asset.id
    delete = AsyncMock(side_effect=[RuntimeError("private sensitive URL"), None, None])
    monkeypatch.setattr(cleanup.storage, "delete_private", delete)
    assert await cleanup.cleanup_assets(sessions) == 0
    async with sessions() as db:
        asset = await db.get(PromptAnalysisAsset, asset_id)
        assert asset.state == "deleting"
    assert await cleanup.cleanup_assets(sessions) == 1
    assert delete.await_count == 3


async def test_expired_history_is_tombstoned_before_result_removed(sessions):
    from app.models import PromptAnalysis
    from app.services.prompt_analysis.cleanup import expire_history

    job, _ = await seed_job(
        sessions,
        status="succeeded",
        generated_result=valid_result(),
        history_retain_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert await expire_history(sessions) == 1
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        assert row.deleted_at is not None and row.generated_result is None
        assert row.status == "succeeded" and row.idempotency_key == job.idempotency_key
    assert await expire_history(sessions) == 0


async def test_live_older_assets_cannot_starve_expired_objects(sessions, monkeypatch):
    import uuid

    from app.models import PromptAnalysisAsset, User
    from app.services.prompt_analysis import cleanup

    now = datetime.now(UTC)
    async with sessions() as db:
        user = User(nickname="cleanup fairness")
        db.add(user)
        await db.flush()
        for number in range(101):
            db.add(
                PromptAnalysisAsset(
                    user_id=user.id,
                    object_key=uuid.uuid4().hex,
                    thumbnail_key=uuid.uuid4().hex,
                    sha256="0" * 64,
                    width=1,
                    height=1,
                    byte_size=1,
                    state="ready",
                    created_at=now - timedelta(days=2 if number < 100 else 1),
                    unbound_expires_at=now + timedelta(days=1) if number < 100 else now - timedelta(hours=1),
                )
            )
        await db.commit()
    delete = AsyncMock()
    monkeypatch.setattr(cleanup.storage, "delete_private", delete)
    assert await cleanup.cleanup_assets(sessions) == 1
    assert delete.await_count == 2
