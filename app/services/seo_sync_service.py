"""Bounded, atomic Google refresh with durable attempts and PostgreSQL locking."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select, text

from app.config import get_settings
from app.models.seo_sync import SeoApiDay, SeoSyncState
from app.services.google_search_console import GoogleSearchConsole, GscError, ServiceAccountToken

LOCK_ID = 73402961
MAX_SECONDS = 600
SAFE_ERRORS = {
    "credentials_unavailable",
    "permission_denied",
    "upstream_unavailable",
    "upstream_rejected",
    "invalid_response",
    "timeout",
    "interrupted",
    "sync_failed",
    "stale_bundle",
    "invalid_bundle",
    "bundle_too_large",
}


def configured():
    settings = get_settings()
    return settings.gsc_enabled and (
        settings.gsc_sync_mode == "external" or bool(settings.gsc_credentials_file)
    )


async def sync_status(db, *, configured=None):
    if configured is None:
        configured = globals()["configured"]()
    state = await db.get(SeoSyncState, 1)
    status = state.status if state else "idle"
    error = state.last_error if state else None
    if (
        state
        and status == "running"
        and datetime.now(UTC) - state.last_attempt_at > timedelta(seconds=MAX_SECONDS + 60)
    ):
        status, error = "failed", "interrupted"
    truncated = bool(
        await db.scalar(select(func.count()).select_from(SeoApiDay).where(SeoApiDay.truncated.is_(True)))
    )
    return {
        "configured": bool(configured),
        "mode": get_settings().gsc_sync_mode,
        "trigger_available": bool(configured) and get_settings().gsc_sync_mode == "server",
        "status": status if configured else "not_configured",
        "last_attempt_at": state.last_attempt_at if state else None,
        "last_success_at": state.last_success_at if state else None,
        "last_error": error if error in SAFE_ERRORS else None,
        "data_through": state.data_through if state else None,
        "truncated": truncated,
    }


async def prepare_sync(sessions):
    """Claim before the HTTP 202 so polling immediately sees durable running state."""
    async with sessions() as db, db.begin():
        if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}):
            return None
        state = await db.get(SeoSyncState, 1)
        now = datetime.now(UTC)
        if (
            state
            and state.status == "running"
            and now - state.last_attempt_at < timedelta(seconds=MAX_SECONDS + 60)
        ):
            return None
        if not state:
            state = SeoSyncState(id=1)
            db.add(state)
        state.status, state.last_attempt_at, state.last_error = "running", now, None
        return now


async def _record_failure(sessions, attempt, code):
    async with sessions() as db, db.begin():
        state = await db.get(SeoSyncState, 1, with_for_update=True)
        if state and state.last_attempt_at == attempt:
            state.status = "failed"
            state.last_error = code if code in SAFE_ERRORS else "sync_failed"


def sync_window(previous, target):
    """Refresh seven previous days and recover up to 21 missing days per bounded run."""
    if previous is None:
        return target, 28
    gap = max(0, (target - previous).days)
    if gap > 21:
        return previous + timedelta(days=21), 28
    return target, min(28, gap + 7)


async def run_sync(
    sessions,
    provider,
    *,
    end: date | None = None,
    days: int | None = None,
    prepared_at=None,
    expected_frontiers=None,
):
    attempt = prepared_at or await prepare_sync(sessions)
    if attempt is None:
        return "busy"
    try:
        async with sessions() as db, db.begin():
            if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}):
                return "busy"
            state = await db.get(SeoSyncState, 1)
            if state.last_attempt_at != attempt:
                return "busy"
            if expected_frontiers is not None and state.data_through not in expected_frontiers:
                raise GscError("stale_bundle")
            end = end or datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=3)
            if days is None:
                end, days = sync_window(state.data_through, end)
            if not 1 <= days <= 28:
                raise GscError("sync_failed")
            start = end - timedelta(days=days - 1)
            items = []
            async with asyncio.timeout(MAX_SECONDS):
                for offset in range(days):
                    day = start + timedelta(days=offset)
                    for dimension in ("daily", "query", "page"):
                        rows, truncated = await provider.fetch_day(day, dimension)
                        items.append(
                            SeoApiDay(
                                day=day,
                                dimension=dimension,
                                rows=rows,
                                truncated=truncated,
                                updated_at=datetime.now(UTC),
                            )
                        )
                await db.execute(delete(SeoApiDay).where(SeoApiDay.day >= start, SeoApiDay.day <= end))
                db.add_all(items)
                state.status, state.last_error = "success", None
                state.last_success_at = datetime.now(UTC)
                state.data_through = max(state.data_through, end) if state.data_through else end
        return "success"
    except asyncio.CancelledError:
        await asyncio.shield(_record_failure(sessions, attempt, "interrupted"))
        raise
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, GscError)
            else "timeout"
            if isinstance(error, TimeoutError)
            else "sync_failed"
        )
        await _record_failure(sessions, attempt, code)
        return "failed"


async def run_configured_sync(prepared_at=None):
    if not configured() or get_settings().gsc_sync_mode != "server":
        return "not_configured"
    from app.database import AsyncSessionLocal

    async with httpx.AsyncClient() as client:
        provider = GoogleSearchConsole(client, ServiceAccountToken(get_settings().gsc_credentials_file))
        return await run_sync(AsyncSessionLocal, provider, prepared_at=prepared_at)


def register_seo_job(scheduler):
    if configured() and get_settings().gsc_sync_mode == "server":
        scheduler.add_job(
            run_configured_sync,
            "cron",
            hour=get_settings().gsc_sync_hour,
            timezone="America/Los_Angeles",
            id="google_search_console",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
