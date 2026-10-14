"""Public category catalog with a consistent publication boundary."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Prompt
from app.utils.category_catalog import CATEGORY_CATALOG
from app.utils.category_catalog import CATEGORY_LABELS as CATEGORY_LABELS
from app.utils.public_prompts import public_prompt_condition

router = APIRouter(prefix="/api/categories", tags=["categories"])


@router.get("")
async def list_categories(db: AsyncSession = Depends(get_db)) -> list[dict]:
    query = select(Prompt.category, func.count()).where(public_prompt_condition()).group_by(Prompt.category)
    counts = dict((await db.execute(query)).all())
    return [
        {**{k: v for k, v in row.items() if k != "boundary"}, "count": counts.get(row["key"], 0)}
        for row in CATEGORY_CATALOG
    ]
