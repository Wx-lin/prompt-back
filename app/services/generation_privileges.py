"""Server-configured generation quota privileges for authenticated user IDs."""

from uuid import UUID

from app.config import get_settings


def unlimited_generation_user_ids() -> frozenset[UUID]:
    configured = getattr(get_settings(), "generation_unlimited_user_ids", "")
    return frozenset(UUID(item.strip()) for item in configured.split(",") if item.strip())
