"""Private same-page creative conversations over existing analysis jobs."""

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.database import get_db
from app.redis import get_redis_client
from app.schemas.prompt_conversation import (
    ConversationInput,
    ConversationMessage,
    PromptAssetImport,
    RetryConversation,
)
from app.services.prompt_analysis import api, conversations, public_sources
from app.services.prompt_analysis.errors import AnalysisError
from app.utils.deps import get_current_user
from app.utils.jwt import decode_access_token_client
from app.utils.prompt_analysis_http import PromptAnalysisRoute, success

router = APIRouter(tags=["prompt-conversations"], route_class=PromptAnalysisRoute)


def creation_headers(request, key):
    try:
        value = str(uuid.UUID(key))
    except (ValueError, TypeError, AttributeError):
        raise AnalysisError(422, "INVALID_IDEMPOTENCY_KEY", "请提供有效的请求标识") from None
    token = request.headers.get("authorization", "").partition(" ")[2]
    return value, decode_access_token_client(token) or "legacy"


@router.post("/api/prompt-assets/from-prompt", status_code=201)
async def import_prompt(
    body: PromptAssetImport,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    redis=Depends(get_redis_client),
):
    return success(
        request, await public_sources.import_prompt_asset(db, redis, user.id, body.prompt_id), status_code=201
    )


@router.post("/api/prompt-conversations", status_code=202)
async def create_conversation(
    body: ConversationInput,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    redis=Depends(get_redis_client),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    key, source = creation_headers(request, idempotency_key)
    job, created = await conversations.create(db, redis, user.id, body, key, source)
    return success(request, await api.detail(db, job), status_code=202 if created else 200)


@router.post("/api/prompt-conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: uuid.UUID,
    body: ConversationMessage,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    redis=Depends(get_redis_client),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    key, source = creation_headers(request, idempotency_key)
    job, created = await conversations.create(
        db, redis, user.id, body, key, source, conversation_id=conversation_id
    )
    return success(request, await api.detail(db, job), status_code=202 if created else 200)


@router.post("/api/prompt-conversations/{conversation_id}/retry", status_code=202)
async def retry_message(
    conversation_id: uuid.UUID,
    body: RetryConversation,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    redis=Depends(get_redis_client),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    key, source = creation_headers(request, idempotency_key)
    job, created = await conversations.retry(
        db, redis, user.id, conversation_id, body.analysis_id, key, source
    )
    return success(request, await api.detail(db, job), status_code=202 if created else 200)


@router.get("/api/prompt-conversations")
async def list_conversations(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
):
    return success(request, await conversations.history(db, user.id, limit, cursor))


@router.get("/api/prompt-conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID, request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    return success(request, await conversations.detail(db, user.id, conversation_id))


@router.delete("/api/prompt-conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, user=Depends(get_current_user), db=Depends(get_db)):
    await conversations.delete(db, user.id, conversation_id)
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})
