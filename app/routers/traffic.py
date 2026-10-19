"""BFF visit ingestion and administrator-only traffic counts."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.traffic import TrafficSummary, VisitInput
from app.services import traffic_service
from app.utils.admin_auth import verify_admin_key

router = APIRouter()


@router.post("/api/analytics/visit", status_code=204, include_in_schema=False)
async def visit(request: Request, db: AsyncSession = Depends(get_db)):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 256:
            raise HTTPException(413, "请求内容过大")
        body.extend(chunk)
    try:
        payload = VisitInput.model_validate(json.loads(body))
    except (ValidationError, ValueError, UnicodeDecodeError):
        # Do not echo the supplied identifier or extra private fields in errors.
        raise HTTPException(422, "无效的访客标识") from None
    await traffic_service.record_visit(db, payload.visitor_hash)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/api/admin/traffic", response_model=TrafficSummary, dependencies=[Depends(verify_admin_key)])
async def traffic(response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return await traffic_service.get_traffic(db)
