"""Concurrency, recovery, source selection and bounded retention of conversations."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import select

from app.models import PromptAnalysis
from app.workers.prompt_analysis import run_once
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import response_data, upload
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation, seed_public_prompt, send_message
from tests.test_prompt_vision import completion_response, install_transport, valid_result

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("AIGC_TEST_DATABASE_URL") or not os.getenv("AIGC_TEST_REDIS_URL"),
        reason="Explicit isolated PostgreSQL and Redis test URLs required",
    ),
]


async def retry(api, first, *, key=None):
    return await api.client.post(
        "/api/prompt-conversations/" + first["conversation_id"] + "/retry",
        headers={**api.owner, "Idempotency-Key": key or str(uuid.uuid4())},
        json={"analysis_id": first["id"]},
    )


async def test_failed_initial_turn_retries_in_same_conversation_once_per_new_key(api, sessions, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx2.Response(429, json={"error": {"message": "private"}})
        return completion_response(json.dumps(valid_result()))

    install_transport(monkeypatch, upstream)
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    key = str(uuid.uuid4())
    second = response_data(await retry(api, first, key=key), 202)
    assert second["conversation_id"] == first["id"] and second["version"] == 2
    assert await run_once(sessions)
    replay = response_data(await retry(api, first, key=key))
    assert replay["id"] == second["id"] and len(calls) == 2
    assert calls[0]["messages"] == calls[1]["messages"]
    error = response_data(await retry(api, second), 409)
    assert error["code"] == "NOT_RETRYABLE"


async def test_unknown_never_retries_or_bypasses_parent_and_keeps_accounting(api, sessions, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx2.Response(503, json={"error": {"message": "private"}})

    install_transport(monkeypatch, upstream)
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    assert response_data(await retry(api, first), 409)["code"] == "CONVERSATION_UNKNOWN"
    assert response_data(await send_message(api, first), 409)["code"] == "CONVERSATION_CHANGED"
    assert not await run_once(sessions) and len(calls) == 1
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        assert job.quota_state == "uncertain"


@pytest.mark.parametrize("target", ["root", "child", "legacy-root"])
async def test_legacy_delete_cannot_partially_remove_a_managed_conversation(
    api, sessions, monkeypatch, target,
):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    if target == "legacy-root":
        first = response_data(await api.client.post(
            "/api/prompt-analyses",
            headers={**api.owner, "Idempotency-Key": str(uuid.uuid4())},
            json={"input_type": "text", "idea_text": "原有单次想法"},
        ), 202)
    else:
        first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    second = response_data(await send_message(api, first), 202)
    assert await run_once(sessions)
    endpoint = "/api/prompt-analyses/" + (second if target == "child" else first)["id"]
    response_data(await api.client.delete(endpoint, headers=api.other), 404)
    assert response_data(await api.client.delete(endpoint, headers=api.owner), 409)["code"] == (
        "CONVERSATION_MANAGED"
    )
    retained = response_data(await api.client.get(
        "/api/prompt-conversations/" + first["id"], headers=api.owner,
    ))
    assert len(retained["turns"]) == 2
    assert all(turn["prompt"] and turn["user_message"] for turn in retained["turns"])
    listing = response_data(await api.client.get("/api/prompt-analyses", headers=api.owner))
    assert [row["id"] for row in listing["items"]] == (
        [first["id"]] if target == "legacy-root" else []
    )
    response_data(await api.client.delete(
        "/api/prompt-conversations/" + first["id"], headers=api.owner,
    ), 204)
    async with sessions() as db:
        for job in await db.scalars(select(PromptAnalysis)):
            assert job.deleted_at is not None
            assert job.user_message is None and job.context_snapshot is None
            assert job.idea_text is None and job.generated_result is None and job.edited_prompts is None


async def test_legacy_delete_does_not_allow_skipping_failed_latest_turn(api, sessions, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(request)
        return (
            completion_response(json.dumps(valid_result())) if len(calls) == 1
            else httpx2.Response(429, json={"error": {"message": "failure"}})
        )

    install_transport(monkeypatch, upstream)
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    second = response_data(await send_message(api, first), 202)
    assert await run_once(sessions)
    assert response_data(await api.client.delete(
        "/api/prompt-analyses/" + second["id"], headers=api.owner,
    ), 409)["code"] == "CONVERSATION_MANAGED"
    assert response_data(await send_message(api, first), 409)["code"] == "CONVERSATION_CHANGED"
    assert len(calls) == 2


async def test_failed_followup_uses_its_frozen_context_when_explicitly_retried(api, sessions, monkeypatch):
    calls = []

    def upstream(request):
        calls.append(json.loads(request.content))
        return (
            httpx2.Response(429, json={"error": {"message": "failure"}})
            if len(calls) == 2
            else completion_response(json.dumps(valid_result()))
        )

    install_transport(monkeypatch, upstream)
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    second = response_data(await send_message(api, first), 202)
    assert await run_once(sessions)
    response_data(
        await api.client.patch(
            "/api/prompt-analyses/" + first["id"],
            headers=api.owner,
            json={"revision": 0, "prompt": {"zh": "后来改了", "en": "Later"}},
        )
    )
    assert response_data(await send_message(api, first), 409)["code"] == "CONVERSATION_CHANGED"
    third = response_data(await retry(api, second), 202)
    assert third["version"] == 3
    assert await run_once(sessions)
    assert calls[1]["messages"] == calls[2]["messages"]


async def test_double_submit_same_key_and_image_create_delete_do_not_deadlock(api, sessions, monkeypatch):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    key = str(uuid.uuid4())
    responses = await asyncio.wait_for(
        asyncio.gather(
            create_conversation(api, key=key),
            create_conversation(api, key=key),
        ),
        timeout=5,
    )
    assert sorted(response.status_code for response in responses) == [200, 202]
    first = responses[0].json()["data"]
    assert await run_once(sessions)
    asset = await upload(api)
    responses = await asyncio.wait_for(
        asyncio.gather(
            api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner),
            create_conversation(api, {"asset_id": asset["id"]}),
        ),
        timeout=5,
    )
    assert [response.status_code for response in responses] == [204, 202]


async def test_same_asset_create_and_conversation_delete_follow_one_lock_order(api, sessions, monkeypatch):
    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    asset = await upload(api)
    first = response_data(await create_conversation(api, {"asset_id": asset["id"]}), 202)
    assert await run_once(sessions)
    responses = await asyncio.wait_for(
        asyncio.gather(
            api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner),
            create_conversation(api, {"asset_id": asset["id"]}),
        ),
        timeout=5,
    )
    assert responses[0].status_code == 204
    assert responses[1].status_code in {202, 404}


async def test_turn_creation_and_completion_never_extend_root_privacy_deadline(api, sessions, monkeypatch):
    from app.services.prompt_analysis import cleanup

    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    first = response_data(await create_conversation(api), 202)
    initial_deadline = first["history_retain_until"]
    assert await run_once(sessions)
    completed = response_data(await api.client.get("/api/prompt-analyses/" + first["id"], headers=api.owner))
    assert completed["history_retain_until"] == initial_deadline
    deadline = datetime.now(UTC) + timedelta(hours=1)
    async with sessions() as db:
        root = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        root.history_retain_until = deadline
        await db.commit()
    child = response_data(await send_message(api, first), 202)
    assert datetime.fromisoformat(child["history_retain_until"]) == deadline
    assert await run_once(sessions)
    child = response_data(await api.client.get("/api/prompt-analyses/" + child["id"], headers=api.owner))
    assert datetime.fromisoformat(child["history_retain_until"]) == deadline

    async def expired_clock(db):
        return deadline + timedelta(seconds=1)

    monkeypatch.setattr(cleanup, "_clock", expired_clock)
    assert await cleanup.expire_history(sessions) == 2
    async with sessions() as db:
        for job in await db.scalars(select(PromptAnalysis)):
            assert job.deleted_at is not None
            assert job.user_message is None and job.context_snapshot is None
            assert job.idea_text is None and job.generated_result is None
    response_data(await send_message(api, child), 404)


async def test_pagination_limit_and_parent_conversation_are_bounded(api, sessions, monkeypatch):
    from app.services.prompt_analysis import conversations

    install_transport(monkeypatch, lambda request: completion_response(json.dumps(valid_result())))
    first = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    second = response_data(await create_conversation(api, {"message": "另一个想法"}), 202)
    assert await run_once(sessions)
    error = response_data(await send_message(api, second, parent_analysis_id=first["id"]), 409)
    assert error["code"] == "CONVERSATION_CHANGED"
    page = response_data(await api.client.get("/api/prompt-conversations?limit=1", headers=api.owner))
    assert page["pagination"]["has_more"]
    other = response_data(
        await api.client.get(
            "/api/prompt-conversations?limit=1&cursor=" + page["pagination"]["next_cursor"],
            headers=api.owner,
        )
    )
    assert {page["items"][0]["id"], other["items"][0]["id"]} == {first["id"], second["id"]}
    response_data(await api.client.get("/api/prompt-conversations?cursor=invalid", headers=api.owner), 422)
    assert response_data(await api.client.get("/api/prompt-conversations", headers=api.other))["items"] == []
    monkeypatch.setattr(conversations, "MAX_TURNS", 1)
    assert response_data(await send_message(api, first), 429)["code"] == "CONVERSATION_LIMIT"


async def test_parent_snapshot_is_not_mutated_after_enqueue_and_recent_messages_are_bounded(
    api,
    sessions,
    configured,
    monkeypatch,
):
    from app.services.prompt_analysis import limits

    configured.prompt_analysis_daily_limit = 20

    async def no_frequency(*args):
        return None

    monkeypatch.setattr(limits, "frequency", no_frequency)
    calls = []
    install_transport(
        monkeypatch,
        lambda request: (
            calls.append(json.loads(request.content)) or completion_response(json.dumps(valid_result()))
        ),
    )
    parent = response_data(await create_conversation(api), 202)
    assert await run_once(sessions)
    for index in range(7):
        previous = parent
        parent = response_data(await send_message(api, parent, f"第{index}次调整"), 202)
        if index == 0:
            response_data(
                await api.client.patch(
                    "/api/prompt-analyses/" + previous["id"],
                    headers=api.owner,
                    json={"revision": 0, "prompt": {"zh": "排队后更改", "en": "Changed after enqueue"}},
                )
            )
        assert await run_once(sessions)
    data = json.loads(calls[-1]["messages"][1]["content"])
    assert len(data["context"]["recent_messages"]) == 6
    assert "排队后更改" not in str(calls[1])


async def test_delete_during_model_call_never_restores_conversation_or_context(api, sessions, monkeypatch):
    from app.services import vision

    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_model(*args, **kwargs):
        entered.set()
        await release.wait()
        return valid_result()

    monkeypatch.setattr(vision, "generate_conversation", wait_model)
    first = response_data(await create_conversation(api), 202)
    task = asyncio.create_task(run_once(sessions))
    await asyncio.wait_for(entered.wait(), timeout=3)
    response_data(await api.client.delete("/api/prompt-conversations/" + first["id"], headers=api.owner), 204)
    release.set()
    assert await asyncio.wait_for(task, timeout=3)
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(first["id"]))
        assert job.deleted_at and job.status == "canceled"
        assert job.context_snapshot is None and job.user_message is None
        assert job.generated_result is None and job.quota_state == "consumed"


async def test_prompt_source_visibility_storage_failure_and_owner_are_enforced(
    api, sessions, configured, monkeypatch
):
    from app.services.prompt_analysis import storage

    configured.cos_bucket, configured.cos_region = "test-bucket", "ap-guangzhou"
    hidden = await seed_public_prompt(
        sessions, "https://test-bucket.cos.ap-guangzhou.myqcloud.com/prompts/a.png", review_status="pending"
    )
    response_data(
        await api.client.post(
            "/api/prompt-assets/from-prompt", headers=api.owner, json={"prompt_id": hidden}
        ),
        404,
    )
    public = await seed_public_prompt(
        sessions, "https://test-bucket.cos.ap-guangzhou.myqcloud.com/prompts/b.png"
    )

    async def unavailable(*args):
        raise RuntimeError("private SDK error")

    monkeypatch.setattr(storage, "read_public_prompt", unavailable)
    assert (
        response_data(
            await api.client.post(
                "/api/prompt-assets/from-prompt", headers=api.owner, json={"prompt_id": public}
            ),
            503,
        )["code"]
        == "STORAGE_UNAVAILABLE"
    )
    asset = await upload(api, headers=api.other)
    response_data(await create_conversation(api, {"asset_id": asset["id"]}), 404)
