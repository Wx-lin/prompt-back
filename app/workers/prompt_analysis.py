"""Dedicated single-concurrency worker: python -m app.workers.prompt_analysis."""

import asyncio
import logging
import signal
import uuid
from contextlib import suppress
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import PromptAnalysis, PromptAnalysisAsset
from app.schemas.prompt_conversation import ConversationImageIntent, ConversationImageQuestion
from app.services import vision
from app.services.prompt_analysis import assets, storage
from app.services.prompt_analysis.cleanup import cleanup_assets, expire_history
from app.services.prompt_analysis.errors import AnalysisError
from app.services.prompt_analysis.jobs import (
    Sessions,
    claim_job,
    dispatch_job,
    finish_job,
    heartbeat_job,
    mark_smart_image,
    recover_jobs,
    smart_followup_allowed,
)
from app.services.vision import image_adapter

logger = logging.getLogger(__name__)
_worker_config_checks = ContextVar("worker_config_checks", default=False)


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


async def _discard_unbound_output(sessions: Sessions, asset_id: str) -> None:
    """Make a fenced or deleted output inaccessible and eligible for cleanup."""
    async with sessions() as db, db.begin():
        asset = await db.scalar(select(PromptAnalysisAsset).where(
            PromptAnalysisAsset.id == uuid.UUID(asset_id)
        ).with_for_update())
        if asset is None:
            return
        bound = await db.scalar(select(exists().where(PromptAnalysis.output_asset_id == asset.id)))
        if not bound:
            asset.state = "deleting"
            asset.purge_after = datetime.now(UTC)


async def _latest_generated_image(sessions: Sessions, claim) -> bytes | None:
    """Read the same user's last visible generated image as optional chat context."""
    from app.services.prompt_analysis.conversations import generated_reference

    if not claim.conversation_id:
        return None
    async with sessions() as db:
        image_id = await generated_reference(db, claim.user_id, claim.conversation_id)
        if not image_id:
            return None
        try:
            asset = await assets.owned_asset(db, claim.user_id, image_id)
        except AnalysisError as exc:
            if exc.status_code == 404:
                return None
            raise
        return await storage.read_generated(asset.object_key)


async def run_once(sessions: Sessions) -> bool:
    """Run one queued item. Callers can inject test storage/provider transports."""
    claim = await claim_job(sessions)
    if claim is None:
        return False
    heartbeat = asyncio.create_task(_heartbeat(sessions, claim))
    dispatched = False
    try:
        settings = get_settings()
        image_mode = getattr(claim, "conversation_mode", "auto") == "image"
        smart_mode = getattr(claim, "conversation_mode", "auto") == "smart"
        execution_timeout = (settings.prompt_image_execution_timeout if image_mode or smart_mode
                             else settings.prompt_analysis_execution_timeout)
        if image_mode:
            if not settings.openai_api_key.get_secret_value().strip():
                raise vision.VisionError("image_model_not_configured")
            storage.require_storage_configuration()
        elif _worker_config_checks.get():
            vision.require_vision_configuration()
        remaining = (
            execution_timeout
            - (datetime.now(UTC) - claim.started_at).total_seconds()
        )
        image = None
        input_type = getattr(claim, "input_type", "image")
        if input_type == "image":
            read_image = storage.read_generated if claim.object_key.endswith(".png") else storage.read_private
            image = await asyncio.wait_for(
                read_image(claim.object_key), timeout=max(0.01, remaining)
            )
        if not await dispatch_job(sessions, claim.id, claim.lease_token):
            await _persist(sessions, claim, error_code="dispatch_fenced")
            return True
        dispatched = True
        remaining = (
            execution_timeout
            - (datetime.now(UTC) - claim.started_at).total_seconds()
        )
        if remaining <= 0:
            await _persist(sessions, claim, error_code="execution_timeout")
            return True
        provider_timeout = (
            settings.prompt_image_call_timeout if image_mode else settings.prompt_analysis_call_timeout
        )
        timeout = min(provider_timeout, remaining)
        image_context = getattr(claim, "context_snapshot", None)
        if smart_mode:
            decision = await asyncio.wait_for(vision.generate_conversation(
                claim.user_message or "", context=claim.context_snapshot, image=image,
                timeout=timeout, mode="smart",
            ), timeout=timeout)
            needs_image_question = isinstance(decision.result, ConversationImageQuestion)
            needs_image_edit = (isinstance(decision.result, ConversationImageIntent)
                                and decision.result.use_reference)
            if (needs_image_question
                    and not await smart_followup_allowed(sessions, claim.id, claim.lease_token)):
                await _persist(sessions, claim, error_code="dispatch_fenced")
                return True
            if (needs_image_question or needs_image_edit) and image is None:
                try:
                    remaining = execution_timeout - (datetime.now(UTC) - claim.started_at).total_seconds()
                    image = await asyncio.wait_for(
                        _latest_generated_image(sessions, claim), timeout=max(0.01, remaining)
                    )
                except Exception:
                    raise vision.VisionError("image_unavailable") from None
                if image is None:
                    raise vision.VisionError("image_unavailable")
            if needs_image_question:
                if not await smart_followup_allowed(sessions, claim.id, claim.lease_token):
                    await _persist(sessions, claim, error_code="dispatch_fenced")
                    return True
                remaining = execution_timeout - (datetime.now(UTC) - claim.started_at).total_seconds()
                if remaining <= 0:
                    raise TimeoutError
                answer = await asyncio.wait_for(vision.generate_conversation(
                    claim.user_message or "", context=claim.context_snapshot, image=image,
                    timeout=min(settings.prompt_analysis_call_timeout, remaining), mode="auto",
                ), timeout=min(settings.prompt_analysis_call_timeout, remaining))
                await _persist(sessions, claim, result=answer)
                return True
            if not isinstance(decision.result, ConversationImageIntent):
                await _persist(sessions, claim, result=decision)
                return True
            if not decision.result.use_reference and not getattr(claim, "selected_generated_image", False):
                image = None
            if not decision.result.use_context:
                image_context = None
            if not settings.openai_api_key.get_secret_value().strip():
                raise vision.VisionError("image_model_not_configured")
            storage.require_storage_configuration()
            if not await mark_smart_image(sessions, claim.id, claim.lease_token):
                await _persist(sessions, claim, error_code="dispatch_fenced")
                return True
            remaining = execution_timeout - (datetime.now(UTC) - claim.started_at).total_seconds()
            if remaining <= 0:
                raise TimeoutError
            timeout = min(settings.prompt_image_call_timeout, remaining)
            image_mode = True
        if image_mode:
            body = await image_adapter.generate(
                image_adapter.build_image_prompt(
                    claim.user_message or "根据参考图片生成一张图片", image_context
                ),
                api_key=settings.openai_api_key.get_secret_value(),
                reference=image, timeout=timeout,
            )
            async with sessions() as db:
                uploaded_id = await assets.store_generated(db, claim.user_id, body)
            result = {"schema_version": "image-1", "title": (claim.user_message or "图片创作")[:80],
                      "asset_id": uploaded_id}
            try:
                await _persist(sessions, claim, result=result)
            finally:
                await _discard_unbound_output(sessions, uploaded_id)
            return True
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
    config_token = _worker_config_checks.set(True)
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
                image_key = getattr(settings, "openai_api_key", None)
                ready = ready or bool(image_key and image_key.get_secret_value().strip())
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
        _worker_config_checks.reset(config_token)
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
