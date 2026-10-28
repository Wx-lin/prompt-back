"""Dedicated single-concurrency worker: python -m app.workers.prompt_analysis."""

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services import vision
from app.services.prompt_analysis import storage
from app.services.prompt_analysis.cleanup import cleanup_assets, expire_history
from app.services.prompt_analysis.jobs import (
    Sessions,
    claim_job,
    dispatch_job,
    finish_job,
    heartbeat_job,
    recover_jobs,
)

logger = logging.getLogger(__name__)


async def _heartbeat(sessions: Sessions, claim) -> None:
    while True:
        await asyncio.sleep(get_settings().prompt_analysis_heartbeat_seconds)
        try:
            if not await heartbeat_job(sessions, claim.id, claim.lease_token):
                return
        except Exception:
            logger.warning("Analysis heartbeat unavailable job_id=%s", claim.id)
            return


async def _persist(sessions: Sessions, claim, **outcome) -> None:
    # Retrying local persistence never repeats the paid request. If persistence
    # stays unavailable the abandoned intent becomes unknown on lease recovery.
    for retry in range(3):
        try:
            await finish_job(sessions, claim.id, claim.lease_token, **outcome)
            return
        except Exception:
            logger.warning("Analysis persistence deferred job_id=%s", claim.id)
            if retry < 2:
                await asyncio.sleep(0.1 * (retry + 1))


async def run_once(sessions: Sessions) -> bool:
    """Run one queued item. Callers can inject test storage/provider transports."""
    claim = await claim_job(sessions)
    if claim is None:
        return False
    heartbeat = asyncio.create_task(_heartbeat(sessions, claim))
    dispatched = False
    try:
        settings = get_settings()
        remaining = (
            settings.prompt_analysis_execution_timeout
            - (datetime.now(UTC) - claim.started_at).total_seconds()
        )
        image = None
        input_type = getattr(claim, "input_type", "image")
        if input_type == "image":
            image = await asyncio.wait_for(
                storage.read_private(claim.object_key), timeout=max(0.01, remaining)
            )
        if not await dispatch_job(sessions, claim.id, claim.lease_token):
            await _persist(sessions, claim, error_code="dispatch_fenced")
            return True
        dispatched = True
        remaining = (
            settings.prompt_analysis_execution_timeout
            - (datetime.now(UTC) - claim.started_at).total_seconds()
        )
        if remaining <= 0:
            await _persist(sessions, claim, error_code="execution_timeout")
            return True
        timeout = min(settings.prompt_analysis_call_timeout, remaining)
        if getattr(claim, "conversation_id", None):
            operation = vision.generate_conversation(
                claim.user_message or "", context=claim.context_snapshot, image=image, timeout=timeout,
                mode=getattr(claim, "conversation_mode", "auto"),
            )
        elif input_type == "text":
            operation = vision.generate_prompt(
                claim.idea_text, purpose=claim.purpose, style=claim.style, timeout=timeout
            )
        else:
            operation = vision.analyze_image(image, timeout=timeout)
        result = await asyncio.wait_for(operation, timeout=timeout)
        await _persist(sessions, claim, result=result)
    except vision.VisionError as exc:
        await _persist(sessions, claim, error_code=exc.code, uncertain=exc.uncertain)
    except TimeoutError:
        await _persist(
            sessions,
            claim,
            error_code="provider_timeout" if dispatched else "image_timeout",
            uncertain=dispatched,
        )
    except asyncio.CancelledError:
        # A process shutdown keeps its intent for conservative lease recovery.
        raise
    except Exception:
        await _persist(sessions, claim, error_code="worker_error", uncertain=dispatched)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    return True


async def run() -> None:
    settings = get_settings()
    if not settings.prompt_analysis_worker_enabled:
        logger.info("Image analysis worker is disabled")
        return
    engine = create_async_engine(
        settings.database_url, echo=False, pool_pre_ping=True, pool_size=2, max_overflow=0
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    last_cleanup = 0.0
    previous_ready = None
    try:
        while not stopped.is_set():
            try:
                await recover_jobs(sessions)
                if loop.time() - last_cleanup >= 60:
                    await expire_history(sessions)
                    await cleanup_assets(sessions)
                    last_cleanup = loop.time()
                try:
                    vision.require_vision_configuration()
                    ready = True
                except Exception:
                    ready = False
                if not ready and previous_ready is not False:
                    logger.warning("Analysis execution unavailable: incomplete configuration")
                previous_ready = ready
                worked = await run_once(sessions) if ready else False
            except Exception:
                logger.warning("Image analysis worker iteration unavailable")
                worked = False
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), timeout=settings.prompt_analysis_poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
