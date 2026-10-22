"""Real PostgreSQL tests for dispatch fencing, recovery and durable results."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import User

pytestmark = pytest.mark.skipif(not os.getenv("AIGC_TEST_DATABASE_URL"), reason="Explicit test DB required")


@pytest_asyncio.fixture
async def sessions():
    schema = "test_prompt_jobs_" + uuid.uuid4().hex
    url = os.environ["AIGC_TEST_DATABASE_URL"]
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with admin.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def seed_job(sessions, **changes):
    from app.config import get_settings
    from app.models import PromptAnalysis, PromptAnalysisAsset

    settings = get_settings()
    now = datetime.now(UTC)
    async with sessions() as db:
        user = User(nickname="private test")
        db.add(user)
        await db.flush()
        asset = PromptAnalysisAsset(
            user_id=user.id,
            object_key=uuid.uuid4().hex + ".jpg",
            thumbnail_key=uuid.uuid4().hex + ".jpg",
            sha256="0" * 64,
            width=10,
            height=10,
            byte_size=10,
            state="ready",
            unbound_expires_at=now + timedelta(hours=24),
        )
        db.add(asset)
        await db.flush()
        values = dict(
            user_id=user.id,
            asset_id=asset.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash="1" * 64,
            source_client="web",
            prompt_version="v1",
            provider=settings.prompt_vision_provider,
            model=settings.prompt_vision_model,
            config_snapshot={"max_tokens": settings.prompt_vision_max_tokens},
            quota_date=now.date(),
            history_retain_until=now + timedelta(days=30),
            image_retain_until=now + timedelta(days=1),
        )
        values.update(changes)
        job = PromptAnalysis(**values)
        db.add(job)
        await db.commit()
        return job, asset


def valid_result():
    return {
        "schema_version": "1",
        "title": "玻璃花瓶",
        "prompt": {"zh": "柔光玻璃花瓶静物摄影", "en": "Softly lit glass vase still life"},
        "breakdown": {key: {"zh": "", "en": ""} for key in ("subject", "composition", "lighting", "style")},
        "uncertain_details": [],
    }


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_dispatch_fences_stale_worker(sessions):
    from app.models import PromptAnalysis, PromptAnalysisAttempt
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, recover_jobs

    job, _ = await seed_job(sessions)
    claimed = await asyncio.gather(claim_job(sessions), claim_job(sessions))
    active = [item for item in claimed if item is not None]
    assert len(active) == 1 and active[0].id == job.id
    token = active[0].lease_token
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await recover_jobs(sessions) == 1
    assert await dispatch_job(sessions, job.id, token) is False
    replacement = await claim_job(sessions)
    assert replacement.lease_token != token
    assert await dispatch_job(sessions, job.id, replacement.lease_token) is True
    assert await dispatch_job(sessions, job.id, replacement.lease_token) is False
    async with sessions() as db:
        attempts = (await db.scalars(select(PromptAnalysisAttempt))).all()
        assert len(attempts) == 1 and attempts[0].dispatch_state == "dispatch_intent"


@pytest.mark.asyncio
async def test_dispatched_lease_expiry_is_unknown_and_never_retried(sessions):
    from app.models import PromptAnalysis
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job, recover_jobs

    job, _ = await seed_job(sessions)
    claimed = await claim_job(sessions)
    assert await dispatch_job(sessions, job.id, claimed.lease_token)
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    await recover_jobs(sessions)
    assert await claim_job(sessions) is None
    assert not await finish_job(sessions, job.id, claimed.lease_token, result=valid_result())
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        assert row.status == "unknown" and row.quota_state == "uncertain"
        assert row.generated_result is None


@pytest.mark.asyncio
async def test_result_and_status_commit_together_and_deleted_result_is_discarded(sessions):
    from app.models import PromptAnalysis, PromptAnalysisAttempt
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job

    for deleted in (False, True):
        job, _ = await seed_job(sessions)
        claim = await claim_job(sessions)
        await dispatch_job(sessions, job.id, claim.lease_token)
        if deleted:
            async with sessions() as db:
                row = await db.get(PromptAnalysis, job.id)
                row.deleted_at = datetime.now(UTC)
                await db.commit()
        assert await finish_job(sessions, job.id, claim.lease_token, result=valid_result())
        async with sessions() as db:
            row = await db.get(PromptAnalysis, job.id)
            attempt = await db.scalar(
                select(PromptAnalysisAttempt).where(PromptAnalysisAttempt.analysis_id == job.id)
            )
            assert row.quota_state == "consumed" and attempt.finished_at is not None
            assert row.status == ("canceled" if deleted else "succeeded")
            assert row.generated_result == (None if deleted else valid_result())
            assert bool(row.deleted_at) is deleted


@pytest.mark.asyncio
async def test_deleted_or_expired_queue_is_never_dispatched_and_releases_quota(sessions):
    from app.models import PromptAnalysis
    from app.services.prompt_analysis.jobs import claim_job

    deleted, _ = await seed_job(sessions, deleted_at=datetime.now(UTC))
    expired, _ = await seed_job(sessions, created_at=datetime.now(UTC) - timedelta(minutes=6))
    assert await claim_job(sessions) is None
    async with sessions() as db:
        first = await db.get(PromptAnalysis, deleted.id)
        second = await db.get(PromptAnalysis, expired.id)
        assert first.status == "canceled" and first.quota_state == "released"
        assert second.status == "failed" and second.error_code == "queue_expired"
        assert second.quota_state == "released"


@pytest.mark.asyncio
async def test_heartbeat_fencing_invalid_asset_and_failure_states(sessions):
    from app.models import PromptAnalysis, PromptAnalysisAsset
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job, finish_job, heartbeat_job

    job, asset = await seed_job(sessions)
    claim = await claim_job(sessions)
    assert not await heartbeat_job(sessions, job.id, uuid.uuid4())
    assert await heartbeat_job(sessions, job.id, claim.lease_token)
    async with sessions() as db:
        stored = await db.get(PromptAnalysisAsset, asset.id)
        stored.state = "deleting"
        await db.commit()
    assert not await dispatch_job(sessions, job.id, claim.lease_token)
    assert await finish_job(sessions, job.id, claim.lease_token, error_code="image_unavailable")
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, job.id)
        assert stored.status == "failed" and stored.quota_state == "released"
    next_job, _ = await seed_job(sessions)
    claim = await claim_job(sessions)
    await dispatch_job(sessions, next_job.id, claim.lease_token)
    assert await finish_job(
        sessions, next_job.id, claim.lease_token, error_code="provider_timeout", uncertain=True
    )
    async with sessions() as db:
        stored = await db.get(PromptAnalysis, next_job.id)
        assert stored.status == "unknown" and stored.quota_state == "uncertain"


@pytest.mark.asyncio
async def test_database_enforces_owner_state_success_result_and_attempt_limits(sessions):
    from app.models import PromptAnalysis, PromptAnalysisAttempt

    job, _ = await seed_job(sessions)
    async with sessions() as db:
        for values in ({"status": "invalid"}, {"revision": -1}, {"status": "succeeded"}):
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    row = await db.get(PromptAnalysis, job.id)
                    for name, value in values.items():
                        setattr(row, name, value)
                    await db.flush()
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(
                    PromptAnalysisAttempt(analysis_id=job.id, attempt_no=3, dispatch_state="dispatch_intent")
                )
                await db.flush()
        other_user = User(nickname="different owner")
        db.add(other_user)
        await db.flush()
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                row = await db.get(PromptAnalysis, job.id)
                row.user_id = other_user.id
                await db.flush()


@pytest.mark.asyncio
async def test_worker_executes_once_and_stores_usage_without_sensitive_error(sessions, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.models import PromptAnalysis, PromptAnalysisAttempt
    from app.workers import prompt_analysis as worker

    job, _ = await seed_job(sessions)
    read = AsyncMock(return_value=b"private bytes")
    analyze = AsyncMock(
        return_value=SimpleNamespace(
            result=valid_result(), provider_request_id="provider-1", usage={"total_tokens": 12}
        )
    )
    monkeypatch.setattr(worker.storage, "read_private", read)
    monkeypatch.setattr(worker.vision, "analyze_image", analyze)
    assert await worker.run_once(sessions)
    assert not await worker.run_once(sessions)
    assert analyze.await_count == 1
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        attempt = await db.scalar(
            select(PromptAnalysisAttempt).where(PromptAnalysisAttempt.analysis_id == job.id)
        )
        assert row.status == "succeeded"
        assert attempt.provider_request_id == "provider-1" and attempt.usage == {"total_tokens": 12}


@pytest.mark.asyncio
async def test_worker_failures_never_retry_provider_and_keep_error_private(sessions, monkeypatch, caplog):
    from unittest.mock import AsyncMock

    from app.models import PromptAnalysis
    from app.workers import prompt_analysis as worker

    monkeypatch.setattr(worker.storage, "read_private", AsyncMock(return_value=b"private"))
    for failure, expected, uncertain in (
        (worker.vision.VisionError("provider_rejected"), "provider_rejected", False),
        (worker.vision.VisionError("provider_timeout", uncertain=True), "provider_timeout", True),
        (TimeoutError("secret signed URL"), "provider_timeout", True),
        (RuntimeError("secret model response"), "worker_error", True),
    ):
        job, _ = await seed_job(sessions)
        analyze = AsyncMock(side_effect=failure)
        monkeypatch.setattr(worker.vision, "analyze_image", analyze)
        assert await worker.run_once(sessions)
        assert analyze.await_count == 1
        async with sessions() as db:
            row = await db.get(PromptAnalysis, job.id)
            assert row.status == ("unknown" if uncertain else "failed")
            assert row.error_code == expected
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_worker_pre_dispatch_failure_and_deletion_never_send(sessions, monkeypatch):
    from unittest.mock import AsyncMock

    from app.models import PromptAnalysis
    from app.workers import prompt_analysis as worker

    analyze = AsyncMock()
    monkeypatch.setattr(worker.vision, "analyze_image", analyze)
    job, _ = await seed_job(sessions)
    monkeypatch.setattr(worker.storage, "read_private", AsyncMock(side_effect=TimeoutError()))
    await worker.run_once(sessions)
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        assert row.status == "failed" and row.quota_state == "released"
    deleted, _ = await seed_job(sessions)

    async def read_and_delete(key):
        async with sessions() as db:
            row = await db.get(PromptAnalysis, deleted.id)
            row.deleted_at = datetime.now(UTC)
            await db.commit()
        return b"image"

    monkeypatch.setattr(worker.storage, "read_private", read_and_delete)
    await worker.run_once(sessions)
    analyze.assert_not_awaited()
    async with sessions() as db:
        row = await db.get(PromptAnalysis, deleted.id)
        assert row.status == "canceled" and row.quota_state == "released"


@pytest.mark.asyncio
async def test_worker_retries_persistence_without_repeating_model(sessions, monkeypatch):
    from unittest.mock import AsyncMock

    from app.models import PromptAnalysis
    from app.workers import prompt_analysis as worker

    job, _ = await seed_job(sessions)
    monkeypatch.setattr(worker.storage, "read_private", AsyncMock(return_value=b"image"))
    analyze = AsyncMock(return_value=valid_result())
    monkeypatch.setattr(worker.vision, "analyze_image", analyze)
    original_finish = worker.finish_job
    calls = 0

    async def temporarily_unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("database unavailable")
        return await original_finish(*args, **kwargs)

    monkeypatch.setattr(worker, "finish_job", temporarily_unavailable)
    await worker.run_once(sessions)
    assert calls == 3 and analyze.await_count == 1
    async with sessions() as db:
        assert (await db.get(PromptAnalysis, job.id)).status == "succeeded"


@pytest.mark.asyncio
async def test_recovery_cancels_deleted_undispatched_and_result_requires_intent(sessions):
    from app.models import PromptAnalysis
    from app.services.prompt_analysis.jobs import (
        claim_job,
        dispatch_job,
        finish_job,
        heartbeat_job,
        recover_jobs,
    )

    job, _ = await seed_job(sessions)
    claim = await claim_job(sessions)
    assert not await finish_job(sessions, job.id, claim.lease_token, result=valid_result())
    assert not await dispatch_job(sessions, uuid.uuid4(), uuid.uuid4())
    assert not await heartbeat_job(sessions, uuid.uuid4(), uuid.uuid4())
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        row.deleted_at = datetime.now(UTC)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert await recover_jobs(sessions) == 1
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        assert row.status == "canceled" and row.quota_state == "released"


@pytest.mark.asyncio
async def test_worker_disabled_and_enabled_entrypoint_use_own_bounded_pool(monkeypatch, sessions):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.workers import prompt_analysis as worker

    settings = SimpleNamespace(prompt_analysis_worker_enabled=False)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    await worker.run()
    settings = SimpleNamespace(
        prompt_analysis_worker_enabled=True,
        database_url="postgresql+asyncpg://test",
        prompt_analysis_poll_seconds=0.01,
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    created = []
    callbacks = []
    monkeypatch.setattr(worker, "create_async_engine", lambda *a, **kw: created.append(kw) or engine)
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *a, **kw: sessions)
    monkeypatch.setattr(worker.vision, "require_vision_configuration", lambda: None)
    monkeypatch.setattr(worker.storage, "require_storage_configuration", lambda: None)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb))
    for name in ("recover_jobs", "expire_history", "cleanup_assets"):
        monkeypatch.setattr(worker, name, AsyncMock())

    async def stop_after_one(_):
        callbacks[0]()
        return False

    monkeypatch.setattr(worker, "run_once", stop_after_one)
    await worker.run()
    assert created == [{"echo": False, "pool_pre_ping": True, "pool_size": 2, "max_overflow": 0}]
    engine.dispose.assert_awaited_once()


def test_analysis_migration_is_additive_and_reversible():
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "alembic/versions/e04b75ad6312_prompt_analysis_jobs.py"
    spec = importlib.util.spec_from_file_location("analysis_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "d91a72f4b308"

    async def run():
        schema = "test_prompt_migration_" + uuid.uuid4().hex
        engine = create_async_engine(os.environ["AIGC_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as db:
                await db.execute(text(f'CREATE SCHEMA "{schema}"'))
                await db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await db.execute(text("CREATE TABLE users (id UUID PRIMARY KEY)"))
                await db.execute(text("CREATE TABLE existing_private_data (id INTEGER PRIMARY KEY)"))
                await db.execute(text("INSERT INTO existing_private_data VALUES (1)"))

                def apply(conn, direction):
                    with Operations.context(MigrationContext.configure(conn)):
                        getattr(migration, direction)()

                await db.run_sync(apply, "upgrade")
                assert await db.scalar(text("SELECT COUNT(*) FROM prompt_analyses")) == 0
                await db.run_sync(apply, "downgrade")
                assert await db.scalar(text("SELECT COUNT(*) FROM existing_private_data")) == 1
                await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_dispatch_rejects_changed_configuration_before_intent(sessions):
    from app.models import PromptAnalysis, PromptAnalysisAttempt
    from app.services.prompt_analysis.jobs import claim_job, dispatch_job

    job, _ = await seed_job(sessions, config_snapshot={"max_tokens": 999999})
    claim = await claim_job(sessions)
    assert not await dispatch_job(sessions, job.id, claim.lease_token)
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        assert row.status == "failed" and row.error_code == "configuration_changed"
        assert row.quota_state == "released"
        assert await db.scalar(select(PromptAnalysisAttempt.id)) is None


@pytest.mark.asyncio
async def test_missing_provider_configuration_still_runs_maintenance(monkeypatch, sessions):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.models import PromptAnalysis
    from app.workers import prompt_analysis as worker

    job, _ = await seed_job(sessions, created_at=datetime.now(UTC) - timedelta(minutes=6))
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            prompt_analysis_worker_enabled=True,
            database_url="postgresql+asyncpg://test",
            prompt_analysis_poll_seconds=0.01,
        ),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(worker, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *a, **kw: sessions)
    callbacks = []
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda s, cb: callbacks.append(cb))

    def missing():
        raise worker.vision.VisionError("not_configured")

    monkeypatch.setattr(worker.vision, "require_vision_configuration", missing)
    monkeypatch.setattr(worker, "cleanup_assets", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(worker, "run_once", execute)

    async def expire_then_stop(_):
        callbacks[0]()
        return 0

    expire = AsyncMock(side_effect=expire_then_stop)
    monkeypatch.setattr(worker, "expire_history", expire)
    await worker.run()
    expire.assert_awaited_once()
    execute.assert_not_awaited()
    async with sessions() as db:
        row = await db.get(PromptAnalysis, job.id)
        assert row.status == "failed" and row.error_code == "queue_expired"
        assert row.quota_state == "released"
