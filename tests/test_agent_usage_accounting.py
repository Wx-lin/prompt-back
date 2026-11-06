"""Keep Agent token accounting when its decision leads to image generation."""

import os
import sys
import uuid
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.models import PromptAnalysisAttempt
from app.schemas.prompt_conversation import ConversationImageIntent
from app.services import vision
from app.services.vision import image_adapter
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import image_bytes, response_data
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis URLs required",
    ),
]


@pytest.mark.parametrize("image_fails", [False, True])
async def test_image_job_records_agent_usage_even_if_image_provider_fails(
    api, sessions, monkeypatch, configured, image_fails,
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    agent = ModuleType("app.services.vision.agent")
    agent.generate_conversation = AsyncMock(return_value=vision.VisionResult(
        ConversationImageIntent(schema_version="image-intent-1", use_reference=False, use_context=False),
        "request-agent-123", {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    ))
    monkeypatch.setitem(sys.modules, agent.__name__, agent)
    monkeypatch.setattr(vision, "agent", agent, raising=False)
    generate = AsyncMock(side_effect=vision.VisionError("image_provider_error") if image_fails else None,
                         return_value=image_bytes())
    monkeypatch.setattr(image_adapter, "generate", generate)

    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "smart"}), 202)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert done["status"] == ("failed" if image_fails else "succeeded")
    async with sessions() as db:
        attempt = await db.scalar(select(PromptAnalysisAttempt).where(
            PromptAnalysisAttempt.analysis_id == uuid.UUID(first["id"]),
        ))
        assert attempt.usage == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}
        assert attempt.provider_request_id == "request-agent-123"


async def test_explicit_legacy_image_job_does_not_require_agent_accounting(
    api, sessions, monkeypatch, configured,
):
    monkeypatch.setattr(configured, "openai_api_key", SecretStr("fixture-only"))
    monkeypatch.setattr(image_adapter, "generate", AsyncMock(return_value=image_bytes()))
    first = response_data(await create_conversation(api, {"message": "画猫", "mode": "image"}), 202)
    assert await run_once(sessions)
    done = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert done["status"] == "succeeded" and done["response_type"] == "image"
    async with sessions() as db:
        attempt = await db.scalar(select(PromptAnalysisAttempt).where(
            PromptAnalysisAttempt.analysis_id == uuid.UUID(first["id"]),
        ))
        assert attempt.usage == {} and attempt.provider_request_id is None
