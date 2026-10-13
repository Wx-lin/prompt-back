"""Model catalog and compatibility with labels stored before normalization."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Prompt
from app.utils.model_labels import MODEL_FAMILIES, model_group, normalize_model_label, normalize_model_labels


async def published_labels(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(func.unnest(Prompt.models)).where(Prompt.is_published.is_(True)).distinct()
    )
    return list(result.scalars().all())


async def matching_stored_labels(db: AsyncSession, requested: list[str]) -> list[str]:
    canonical = normalize_model_labels(requested)
    stored = await published_labels(db)
    return list(
        dict.fromkeys([*canonical, *[label for label in stored if normalize_model_label(label) in canonical]])
    )


async def list_models(db: AsyncSession) -> list[dict[str, str]]:
    labels = normalize_model_labels([*MODEL_FAMILIES, *await published_labels(db)])
    labels = sorted(labels, key=lambda label: (model_group(label), label.casefold()))
    return [{"key": label, "label": label, "group": model_group(label)} for label in labels]
