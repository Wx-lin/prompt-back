"""Public model catalog shared by gallery filters and submission forms."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import model_service

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("")
async def list_models(db: AsyncSession = Depends(get_db)) -> list[dict[str, str]]:
    return await model_service.list_models(db)
