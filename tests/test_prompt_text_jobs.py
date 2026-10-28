"""Text prompt jobs share dispatch fencing without touching private images."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from tests.test_prompt_jobs import sessions as _sessions
from tests.test_prompt_jobs import valid_result

sessions = _sessions


@pytest.mark.asyncio
async def test_text_worker_dispatches_once_without_reading_storage(monkeypatch):
    from app.workers import prompt_analysis as worker

    claim = SimpleNamespace(
        id=uuid.uuid4(), lease_token=uuid.uuid4(), object_key=None,
        started_at=datetime.now(UTC), input_type="text",
        idea_text="雨夜送花的小猫", purpose="wallpaper", style="cinematic",
    )
    monkeypatch.setattr(worker, "claim_job", AsyncMock(side_effect=[claim, None]))
    monkeypatch.setattr(worker, "dispatch_job", AsyncMock(return_value=True))
    persisted = AsyncMock()
    monkeypatch.setattr(worker, "finish_job", persisted)
    storage_read = AsyncMock(side_effect=AssertionError("text must not read images"))
    monkeypatch.setattr(worker.storage, "read_private", storage_read)
    generate = AsyncMock(return_value=valid_result())
    monkeypatch.setattr(worker.vision, "generate_prompt", generate, raising=False)
    assert await worker.run_once(None)
    assert not await worker.run_once(None)
    storage_read.assert_not_awaited()
    generate.assert_awaited_once()
    assert generate.call_args.args == (claim.idea_text,)
    assert generate.call_args.kwargs["purpose"] == "wallpaper"
    assert generate.call_args.kwargs["style"] == "cinematic"
    assert "result" in persisted.call_args.kwargs


@pytest.mark.asyncio
async def test_text_worker_unknown_outcome_never_repeats_provider(monkeypatch):
    from app.workers import prompt_analysis as worker

    claim = SimpleNamespace(
        id=uuid.uuid4(), lease_token=uuid.uuid4(), object_key=None,
        started_at=datetime.now(UTC), input_type="text",
        idea_text="玻璃花瓶", purpose=None, style=None,
    )
    monkeypatch.setattr(worker, "claim_job", AsyncMock(return_value=claim))
    monkeypatch.setattr(worker, "dispatch_job", AsyncMock(return_value=True))
    persisted = AsyncMock()
    monkeypatch.setattr(worker, "finish_job", persisted)
    generate = AsyncMock(side_effect=worker.vision.VisionError("model_timeout", uncertain=True))
    monkeypatch.setattr(worker.vision, "generate_prompt", generate, raising=False)
    monkeypatch.setattr(worker.storage, "read_private", AsyncMock())
    assert await worker.run_once(None)
    generate.assert_awaited_once()
    assert persisted.call_args.kwargs == {"error_code": "model_timeout", "uncertain": True}


async def seed_text(sessions, **changes):
    from app.config import get_settings
    from app.models import PromptAnalysis, User

    settings = get_settings()
    now = datetime.now(UTC)
    async with sessions() as db:
        user = User(nickname="text worker owner")
        db.add(user)
        await db.flush()
        values = {
            "user_id": user.id, "asset_id": None, "input_type": "text",
            "idea_text": "雨夜送花的小猫", "purpose": "wallpaper", "style": "cinematic",
            "idempotency_key": uuid.uuid4().hex, "request_hash": "1" * 64,
            "source_client": "wechat", "prompt_version": "text-v1",
            "provider": settings.prompt_vision_provider, "model": settings.prompt_vision_model,
            "config_snapshot": {"prompt_version": "text-v1", "max_tokens": settings.prompt_vision_max_tokens},
            "quota_date": now.date(), "history_retain_until": now + timedelta(days=30),
            "image_retain_until": now + timedelta(days=1),
        }
        values.update(changes)
        job = PromptAnalysis(**values)
        db.add(job)
        await db.commit()
        return job


db_required = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit test DB required")


@db_required
@pytest.mark.asyncio
async def test_assetless_text_claim_dispatch_and_result_are_durable(sessions):
    from app.models import PromptAnalysis, PromptAnalysisAttempt
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job

    job = await seed_text(sessions)
    claim = await claim_job(sessions)
    assert claim is not None and claim.id == job.id
    assert claim.input_type == "text" and claim.object_key is None
    assert (claim.idea_text, claim.purpose, claim.style) == (job.idea_text, "wallpaper", "cinematic")
    assert await dispatch_job(sessions, job.id, claim.lease_token)
    assert not await dispatch_job(sessions, job.id, claim.lease_token)
    assert await finish_job(sessions, job.id, claim.lease_token, result=valid_result())
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job.id)
        assert stored.status == "succeeded" and stored.quota_state == "consumed"
        attempts = list(await db.scalars(select(PromptAnalysisAttempt)))
        assert len(attempts) == 1 and attempts[0].dispatch_state == "succeeded"


@db_required
@pytest.mark.asyncio
async def test_text_lease_recovery_keeps_uncertain_dispatch(sessions):
    from app.models import PromptAnalysis
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, recover_jobs

    job = await seed_text(sessions)
    claim = await claim_job(sessions)
    assert await dispatch_job(sessions, job.id, claim.lease_token)
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job.id)
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await recover_jobs(sessions) == 1
    assert await claim_job(sessions) is None
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job.id)
        assert stored.status == "unknown" and stored.quota_state == "uncertain"


@db_required
@pytest.mark.asyncio
async def test_expired_text_history_clears_private_input_and_preserves_accounting(sessions):
    from app.models import PromptAnalysis
    from app.services.prompt_analysis.cleanup import expire_history

    job = await seed_text(
        sessions, status="succeeded", generated_result=valid_result(), quota_state="consumed",
        history_retain_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert await expire_history(sessions) == 1
    assert await expire_history(sessions) == 0
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job.id)
        assert stored.deleted_at is not None and stored.generated_result is None
        assert stored.idea_text is None and stored.purpose is None and stored.style is None
        assert stored.request_hash == job.request_hash and stored.idempotency_key == job.idempotency_key
        assert stored.quota_state == "consumed"
