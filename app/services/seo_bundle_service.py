"""External runners export bounded final source data; the server never fetches Google."""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.seo_bundle import SeoBundle, SeoPartition
from app.services.google_search_console import GscError
from app.services.seo_sync_service import MAX_SECONDS, configured, run_sync, sync_window

MAX_BUNDLE_BYTES = 64_000_000


def final_target():
    return datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=3)


def parse_bundle(raw):
    if len(raw) > MAX_BUNDLE_BYTES:
        raise GscError("bundle_too_large")
    try:
        return SeoBundle.model_validate_json(raw)
    except (ValidationError, ValueError):
        raise GscError("invalid_bundle") from None


async def export_bundle(provider, *, previous: date | None, target: date | None = None):
    target = target or final_target()
    if previous is not None and previous > target:
        raise GscError("invalid_bundle")
    end, days = sync_window(previous, target)
    start = end - timedelta(days=days - 1)
    partitions, size = [], 0
    async with asyncio.timeout(MAX_SECONDS):
        for offset in range(days):
            day = start + timedelta(days=offset)
            for dimension in ("daily", "query", "page"):
                rows, truncated = await provider.fetch_day(day, dimension)
                try:
                    part = SeoPartition(day=day, dimension=dimension, rows=rows, truncated=truncated)
                except ValidationError:
                    raise GscError("invalid_bundle") from None
                size += len(part.model_dump_json().encode())
                if size > MAX_BUNDLE_BYTES:
                    raise GscError("bundle_too_large")
                partitions.append(part)
    bundle = SeoBundle(
        version=1,
        site_url="https://aigc-prompt.com/",
        search_type="web",
        data_state="final",
        previous_data_through=previous,
        target_end=target,
        start_date=start,
        end_date=end,
        partitions=partitions,
    )
    raw = bundle.model_dump_json().encode()
    if len(raw) > MAX_BUNDLE_BYTES:
        raise GscError("bundle_too_large")
    return raw


class BundleProvider:
    def __init__(self, bundle):
        self.parts = {(part.day, part.dimension): part for part in bundle.partitions}

    async def fetch_day(self, day, dimension):
        part = self.parts[(day, dimension)]
        return [row.model_dump() for row in part.rows], part.truncated


async def import_bundle(sessions, raw):
    if not configured() or get_settings().gsc_sync_mode != "external":
        raise GscError("external_not_configured")
    bundle = parse_bundle(raw)
    if bundle.target_end > final_target():
        raise GscError("invalid_bundle")
    result = await run_sync(
        sessions,
        BundleProvider(bundle),
        end=bundle.end_date,
        days=(bundle.end_date - bundle.start_date).days + 1,
        expected_frontiers=(bundle.previous_data_through, bundle.end_date),
    )
    return result


async def record_external_failure(sessions, attempt_at, code):
    """A late runner failure must never overwrite a newer successful import or attempt."""
    from datetime import UTC

    from sqlalchemy import text

    from app.models.seo_sync import SeoSyncState
    from app.services.seo_sync_service import LOCK_ID, SAFE_ERRORS

    if not configured() or get_settings().gsc_sync_mode != "external":
        raise GscError("external_not_configured")
    now = datetime.now(UTC)
    if (
        code not in SAFE_ERRORS
        or attempt_at.tzinfo is None
        or attempt_at > now + timedelta(seconds=60)
        or attempt_at < now - timedelta(days=2)
    ):
        raise GscError("invalid_bundle")
    async with sessions() as db, db.begin():
        if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}):
            return False
        state = await db.get(SeoSyncState, 1)
        if state and (
            (state.last_attempt_at and state.last_attempt_at >= attempt_at)
            or (state.last_success_at and state.last_success_at >= attempt_at)
        ):
            return False
        if not state:
            state = SeoSyncState(id=1)
            db.add(state)
        state.status, state.last_attempt_at, state.last_error = "failed", attempt_at, code
        return True
