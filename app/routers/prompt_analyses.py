"""Shared image-to-prompt API. All business state belongs to the signed user."""

import uuid

from fastapi import APIRouter, Depends, File, Header, Query, Request, Response, UploadFile
from starlette.responses import JSONResponse

from app.database import get_db
from app.redis import get_redis_client
from app.schemas.prompt_analysis import CreateAnalysisRequest, EditAnalysisRequest
from app.services.prompt_analysis import api, assets
from app.services.prompt_analysis.errors import AnalysisError
from app.utils.deps import get_current_user
from app.utils.jwt import decode_access_token_client
from app.utils.prompt_analysis_http import PromptAnalysisRoute, request_id, success

router = APIRouter(tags=["image-to-prompt"], route_class=PromptAnalysisRoute)


@router.post("/api/prompt-assets", status_code=201)
async def upload_asset(
    request: Request,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
    redis=Depends(get_redis_client),
    file: UploadFile = File(...),
):
    return success(request, await assets.upload(db, redis, current_user.id, file), status_code=201)


@router.get("/api/prompt-assets/{asset_id}")
async def get_asset(
    asset_id: uuid.UUID, request: Request, current_user=Depends(get_current_user), db=Depends(get_db)
):
    return success(request, await assets.asset_data(await assets.owned_asset(db, current_user.id, asset_id)))


@router.post("/api/prompt-analyses", status_code=202)
async def create_analysis(
    body: CreateAnalysisRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
    redis=Depends(get_redis_client),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    try:
        if idempotency_key is None:
            raise ValueError
        idempotency_key = str(uuid.UUID(idempotency_key))
    except ValueError:
        raise AnalysisError(422, "INVALID_IDEMPOTENCY_KEY", "请提供有效的请求标识") from None
    token = request.headers.get("authorization", "").partition(" ")[2]
    source = decode_access_token_client(token) or "legacy"
    job, created = await api.create(
        db, redis, current_user.id, body.asset_id, idempotency_key, source,
        input_type=body.input_type, idea_text=body.idea_text, purpose=body.purpose, style=body.style,
    )
    return success(request, await api.detail(db, job), status_code=202 if created else 200)


@router.get("/api/prompt-analyses")
async def list_history(
    request: Request,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None, max_length=512),
):
    return success(request, await api.history(db, current_user.id, limit, cursor))


@router.get("/api/prompt-analyses/{analysis_id}")
async def get_analysis(
    analysis_id: uuid.UUID, request: Request, current_user=Depends(get_current_user), db=Depends(get_db)
):
    return success(request, await api.detail(db, await api.owned_job(db, current_user.id, analysis_id)))


@router.patch("/api/prompt-analyses/{analysis_id}")
async def edit_analysis(
    analysis_id: uuid.UUID,
    body: EditAnalysisRequest,
    request: Request,
    current_user=Depends(get_current_user),
    db=Depends(get_db),
):
    job = await api.edit(db, current_user.id, analysis_id, body.revision, body.prompt.model_dump())
    return success(request, await api.detail(db, job))


@router.delete("/api/prompt-analyses/{analysis_id}", status_code=204)
async def delete_analysis(analysis_id: uuid.UUID, current_user=Depends(get_current_user), db=Depends(get_db)):
    await api.delete(db, current_user.id, analysis_id)
    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.get("/api/prompt-examples")
async def examples(request: Request):
    # Curated sample assets/results are not available yet. Never invent live results.
    return JSONResponse(
        {"success": True, "data": {"items": []}, "error": None, "request_id": request_id(request)},
        headers={"Cache-Control": "public, max-age=300"},
    )
