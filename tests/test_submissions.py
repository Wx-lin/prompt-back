"""Submission input and direct API body bounds."""

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.schemas.submission import SubmissionCreate, SubmissionReject
from app.utils.submission_body_limit import SubmissionBodyLimit


def payload(**changes):
    return {
        "title": "作品",
        "prompt_text": "red cat",
        "category": "portrait",
        "models": ["MJ V6"],
        "upload_ids": [str(uuid.uuid4())],
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"title": "   "},
        {"prompt_text": " "},
        {"category": "invalid"},
        {"models": ["SD " + "x" * 47]},
        {"models": []},
        {"models": [" "]},
        {"is_published": True},
        {"submitter_id": str(uuid.uuid4())},
        {"upload_ids": []},
        {"upload_ids": [str(uuid.UUID(int=1))] * 2},
    ],
)
def test_submission_rejects_invalid_or_server_owned_fields(change):
    with pytest.raises(ValidationError):
        SubmissionCreate(**payload(**change))


def test_rejection_requires_reason():
    with pytest.raises(ValidationError):
        SubmissionReject(note="   ")


def test_direct_api_rejects_large_chunked_body_before_handler():
    app = FastAPI()
    app.add_middleware(SubmissionBodyLimit)

    @app.post("/api/submissions")
    def submit():
        pytest.fail("oversized body reached handler")

    async def run():
        async def body():
            yield b"x" * 100_000
            yield b"x" * 100_000

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/submissions", content=body())
            assert response.status_code == 413

    asyncio.run(run())


def test_upload_rate_limit_and_cache_policy():
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.utils.submission_limits import enforce_limit

    async def run():
        redis = AsyncMock()
        redis.eval.return_value = 31
        with pytest.raises(HTTPException) as error:
            await enforce_limit(redis, uuid.uuid4(), "upload", 30, 3600)
        assert error.value.status_code == 429
        redis.eval.side_effect = ConnectionError()
        with pytest.raises(HTTPException) as error:
            await enforce_limit(redis, uuid.uuid4(), "upload", 30, 3600)
        assert error.value.status_code == 503
        app = FastAPI()
        app.add_middleware(SubmissionBodyLimit)

        @app.get("/api/submissions")
        async def mine():
            return {"items": []}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            result = await client.get("/api/submissions")
            assert result.headers["cache-control"] == "private, no-store"

    asyncio.run(run())
