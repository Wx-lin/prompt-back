"""DashScope configuration, durable snapshots and numeric accounting only."""

import os
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import PromptAnalysis, PromptAnalysisAttempt
from app.services.prompt_analysis.jobs import _result_parts, claim_job, dispatch_job, finish_job
from tests.test_prompt_api_integration import (
    api as api,
)
from tests.test_prompt_api_integration import (
    configured as configured,
)
from tests.test_prompt_api_integration import create, generated_result, response_data, upload
from tests.test_prompt_api_integration import (
    limiter as limiter,
)
from tests.test_prompt_api_integration import (
    objects as objects,
)
from tests.test_prompt_api_integration import (
    sessions as sessions,
)

integration = pytest.mark.skipif(
    not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
    reason="Explicit isolated PostgreSQL and Redis test URLs required",
)


def test_provider_choices_are_explicit_and_defaults_remain_disabled():
    settings = Settings(_env_file=None)
    assert settings.prompt_vision_provider == "openai-compatible"
    assert settings.prompt_vision_model == ""
    assert settings.prompt_analysis_enabled is False
    assert settings.prompt_analysis_worker_enabled is False
    assert Settings(_env_file=None, prompt_vision_provider="dashscope").prompt_vision_provider == "dashscope"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, prompt_vision_provider="mock")


def test_result_accounting_allows_only_nonnegative_numeric_usage_details():
    expected = {
        "prompt_tokens": 40,
        "completion_tokens": 50,
        "total_tokens": 90,
        "image_tokens": 30,
        "text_input_tokens": 10,
        "cached_tokens": 3,
        "reasoning_tokens": 8,
        "text_output_tokens": 42,
    }
    result = SimpleNamespace(
        result=generated_result(),
        provider_request_id="request-test",
        usage={**expected, "provider_body": "private", "input_tokens": True, "output_tokens": -1},
    )
    content, request_id, usage = _result_parts(result)
    assert content == generated_result()
    assert request_id == "request-test"
    assert usage == expected


@pytest.mark.asyncio
@integration
@pytest.mark.parametrize("provider", ["dashscope", "openai-compatible"])
async def test_creation_snapshots_provider_options_and_persists_detailed_usage(
    api, sessions, configured, provider, monkeypatch
):
    monkeypatch.setattr(configured, "prompt_vision_provider", provider)
    monkeypatch.setattr(configured, "prompt_vision_model", "qwen3.8-max")
    asset = await upload(api)
    created = response_data(await create(api, asset["id"]), 202)
    job_id = uuid.UUID(created["id"])
    async with sessions() as db:
        job = await db.get(PromptAnalysis, job_id)
        assert job.config_snapshot["provider"] == provider
        assert job.config_snapshot["model"] == "qwen3.8-max"
        if provider == "dashscope":
            assert job.config_snapshot["enable_thinking"] is False
        else:
            assert "enable_thinking" not in job.config_snapshot
        assert "test-no-provider-call" not in str(job.config_snapshot)
    claimed = await claim_job(sessions)
    assert await dispatch_job(sessions, job_id, claimed.lease_token)
    usage = {"total_tokens": 90, "image_tokens": 30, "reasoning_tokens": 0, "text_output_tokens": 50}
    assert await finish_job(
        sessions, job_id, claimed.lease_token,
        result=SimpleNamespace(result=generated_result(), usage=usage, provider_request_id="request-test"),
    )
    async with sessions() as db:
        attempt = await db.scalar(
            select(PromptAnalysisAttempt).where(PromptAnalysisAttempt.analysis_id == job_id)
        )
        assert attempt.usage == usage


@pytest.mark.asyncio
@integration
@pytest.mark.parametrize("change", ["thinking_snapshot", "provider"])
async def test_dispatch_rejects_configuration_change_before_billable_intent(
    api, sessions, configured, monkeypatch, change
):
    monkeypatch.setattr(configured, "prompt_vision_provider", "dashscope")
    monkeypatch.setattr(configured, "prompt_vision_model", "qwen3.8-max")
    asset = await upload(api)
    created = response_data(await create(api, asset["id"]), 202)
    job_id = uuid.UUID(created["id"])
    if change == "thinking_snapshot":
        async with sessions() as db:
            job = await db.get(PromptAnalysis, job_id)
            job.config_snapshot = {**job.config_snapshot, "enable_thinking": True}
            await db.commit()
    else:
        monkeypatch.setattr(configured, "prompt_vision_provider", "openai-compatible")
    claimed = await claim_job(sessions)
    assert not await dispatch_job(sessions, job_id, claimed.lease_token)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, job_id)
        assert job.error_code == "configuration_changed"
        assert job.quota_state == "released"
        assert await db.scalar(select(PromptAnalysisAttempt.id)) is None
