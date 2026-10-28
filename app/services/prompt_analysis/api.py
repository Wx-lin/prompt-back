"""Task creation, quotas and private history, independent of HTTP clients."""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models import PromptAnalysis, PromptAnalysisAsset
from app.services import vision
from app.services.prompt_analysis import assets, limits
from app.services.prompt_analysis.errors import AnalysisError
from app.services.vision.conversation_adapter import CHAT_PROMPT_VERSION
from app.services.vision.text_adapter import TEXT_PROMPT_VERSION

FEATURE_LOCK = 752056682
_PROMPT_NUMBER_UNSET = object()


async def budget_lock(db):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": FEATURE_LOCK})


def _existing(existing, request_hash):
    if existing.deleted_at is not None or existing.history_retain_until <= datetime.now(UTC):
        raise assets.not_found()
    if existing.request_hash != request_hash:
        raise AnalysisError(409, "IDEMPOTENCY_CONFLICT", "请为新的解析请求使用新的请求标识")
    return existing


async def create(
    db, redis, user_id, asset_id, idempotency_key, source_client, *,
    input_type="image", idea_text=None, purpose=None, style=None,
    chat=False, conversation_id=None, parent_analysis_id=None, parent_revision=None,
    user_message=None, retry_analysis_id=None, conversation_mode="auto",
):
    # Check replay before feature/config/version changes or new reservations.
    payload = {"asset_id": str(asset_id)} if input_type == "image" else {
        "input_type": "text", "idea_text": idea_text, "purpose": purpose, "style": style,
    }
    if chat:
        if conversation_mode not in {"auto", "generate"}:
            raise AnalysisError(422, "INVALID_REQUEST", "对话模式无效")
        payload = {**payload, "conversation": True, "conversation_id": str(conversation_id),
                   "parent_analysis_id": str(parent_analysis_id), "parent_revision": parent_revision,
                   "message": user_message, "retry_analysis_id": str(retry_analysis_id)}
        # Omitted/auto keeps the existing canonical digest for uncertain old
        # requests; explicit generation is a different idempotent operation.
        if conversation_mode != "auto":
            payload = {**payload, "mode": conversation_mode}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def prior():
        return await db.scalar(
            select(PromptAnalysis).where(
                PromptAnalysis.user_id == user_id,
                PromptAnalysis.idempotency_key == idempotency_key,
            )
        )

    existing = await prior()
    if existing:
        return _existing(existing, digest), False
    assets.ensure_enabled()
    try:
        vision.require_vision_configuration()
    except vision.VisionError:
        raise AnalysisError(503, "MODEL_UNAVAILABLE", "提示词生成暂未配置，请稍后再试") from None
    await limits.frequency(redis, f"create:{user_id}", 10, 60)
    await budget_lock(db)
    existing = await prior()
    if existing:
        await db.commit()
        return _existing(existing, digest), False
    asset = await assets.owned_asset(db, user_id, asset_id, lock=True) if input_type == "image" else None
    now = datetime.now(UTC)
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    settings = get_settings()
    active = await db.scalar(
        select(
            exists().where(
                PromptAnalysis.user_id == user_id,
                PromptAnalysis.status.in_(["queued", "running"]),
            )
        )
    )
    if active:
        raise AnalysisError(409, "ANALYSIS_IN_PROGRESS", "已有任务正在解析，请稍后再试")
    budget_filter = (
        PromptAnalysis.quota_date == day,
        PromptAnalysis.quota_state.in_(["reserved", "consumed", "uncertain"]),
    )
    total = await db.scalar(select(func.count()).select_from(PromptAnalysis).where(*budget_filter))
    personal = await db.scalar(
        select(func.count())
        .select_from(PromptAnalysis)
        .where(*budget_filter, PromptAnalysis.user_id == user_id)
    )
    if (
        total >= settings.prompt_analysis_global_daily_limit
        or personal >= settings.prompt_analysis_daily_limit
    ):
        raise AnalysisError(429, "QUOTA_EXCEEDED", "今日解析次数已用完")
    queued = await db.scalar(
        select(func.count()).select_from(PromptAnalysis).where(PromptAnalysis.status == "queued")
    )
    if queued >= settings.prompt_analysis_queue_limit:
        raise AnalysisError(503, "QUEUE_FULL", "当前解析任务较多，请稍后重试")
    conversation_fields = {}
    job_id = uuid.uuid4()
    retain_until = now + timedelta(days=30)
    if chat:
        from app.services.prompt_analysis.conversations import prepare
        prepared = await prepare(
            db, user_id, conversation_id, parent_analysis_id, parent_revision,
            retry_id=retry_analysis_id,
        )
        retain_until = prepared.get("history_retain_until", retain_until)
        conversation_fields = {"conversation_id": conversation_id or job_id,
                               "user_message": user_message or "",
                               **{key: value for key, value in prepared.items()
                                  if key != "history_retain_until"}}
    prompt_version = (
        CHAT_PROMPT_VERSION if chat else
        TEXT_PROMPT_VERSION if input_type == "text" else settings.prompt_analysis_prompt_version
    )
    job = PromptAnalysis(
        id=job_id,
        user_id=user_id,
        asset_id=asset.id if asset else None,
        input_type=input_type,
        idea_text=idea_text,
        purpose=purpose,
        style=style,
        **conversation_fields,
        idempotency_key=idempotency_key,
        request_hash=digest,
        source_client=source_client,
        prompt_version=prompt_version,
        provider=settings.prompt_vision_provider,
        model=settings.prompt_vision_model,
        config_snapshot={
            "provider": settings.prompt_vision_provider,
            "model": settings.prompt_vision_model,
            "base_url": settings.prompt_vision_base_url,
            "prompt_version": prompt_version,
            "max_tokens": settings.prompt_vision_max_tokens,
            "credential_ref": "PROMPT_VISION_API_KEY",
            **({"conversation_mode": conversation_mode} if chat else {}),
            **({"enable_thinking": False} if settings.prompt_vision_provider == "dashscope" else {}),
        },
        quota_date=day,
        history_retain_until=retain_until,
        image_retain_until=now + timedelta(hours=24),
    )
    db.add(job)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await prior()
        if existing:
            return _existing(existing, digest), False
        raise
    await db.refresh(job)
    return job, True


async def owned_job(db, user_id, job_id, *, lock=False):
    query = select(PromptAnalysis).where(
        PromptAnalysis.id == job_id,
        PromptAnalysis.user_id == user_id,
        PromptAnalysis.deleted_at.is_(None),
        PromptAnalysis.history_retain_until > datetime.now(UTC),
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    job = await db.scalar(query)
    if job is None:
        raise assets.not_found()
    return job


async def image_fields(db, job):
    if job.asset_id is None:
        return {
            "image_available": False,
            "preview_url": None,
            "thumbnail_url": None,
            "preview_expires_at": None,
        }
    asset = await db.get(PromptAnalysisAsset, job.asset_id)
    if (
        asset is None
        or job.image_retain_until <= datetime.now(UTC)
        or not await assets.is_available(db, asset)
    ):
        return {
            "image_available": False,
            "preview_url": None,
            "thumbnail_url": None,
            "preview_expires_at": None,
        }
    try:
        return {"image_available": True, **await assets.preview_fields(asset)}
    except AnalysisError:
        return {
            "image_available": True,
            "preview_url": None,
            "thumbnail_url": None,
            "preview_expires_at": None,
            "preview_status": "temporarily_unavailable",
        }


async def detail(db, job, *, summary=False, prompt_number=_PROMPT_NUMBER_UNSET):
    result = job.generated_result
    assistant = bool(result and result.get("schema_version") == "reply-1")
    prompt = None if assistant else job.edited_prompts or (result.get("prompt") if result else None)
    value = {
        "id": str(job.id),
        "asset_id": str(job.asset_id) if job.asset_id else None,
        "input_type": job.input_type,
        "idea_text": job.idea_text,
        "purpose": job.purpose,
        "style": job.style,
        "conversation_id": str(job.conversation_id or job.id),
        "parent_analysis_id": str(job.parent_analysis_id) if job.parent_analysis_id else None,
        "user_message": job.user_message if job.user_message is not None else (job.idea_text or ""),
        "version": job.conversation_turn or 1,
        "status": job.status,
        "phase": job.phase,
        "revision": job.revision,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "title": result.get("title") if result else None,
        "error_code": job.error_code,
        "history_retain_until": job.history_retain_until.isoformat(),
        **await image_fields(db, job),
    }
    if summary:
        value["summary"] = {language: content[:160] for language, content in (prompt or {}).items()}
    else:
        value.update({
            "result": None if assistant else result,
            "prompt": prompt,
            "saved_to_history": job.status == "succeeded",
        })
        if job.conversation_id:
            value.update(await _conversation_output(db, job, assistant, prompt, prompt_number))
    return value


async def _conversation_output(db, job, assistant, prompt, prompt_number):
    ready = job.status == "succeeded"
    number = None
    if ready and not assistant and prompt:
        number = prompt_number
        if number is _PROMPT_NUMBER_UNSET:
            group = job.conversation_id or job.id
            number = await db.scalar(select(func.count()).select_from(PromptAnalysis).where(
                PromptAnalysis.user_id == job.user_id,
                or_(PromptAnalysis.conversation_id == group, PromptAnalysis.id == group),
                PromptAnalysis.deleted_at.is_(None),
                PromptAnalysis.history_retain_until > datetime.now(UTC),
                PromptAnalysis.status == "succeeded",
                func.coalesce(PromptAnalysis.conversation_turn, 1) <= (job.conversation_turn or 1),
                PromptAnalysis.generated_result["schema_version"].astext == "1",
            ))
    return {
        "response_type": ("assistant" if assistant else "prompt") if ready else None,
        "assistant_reply": job.generated_result["reply"] if ready and assistant else None,
        "prompt_number": number,
    }


def decode_cursor(cursor):
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        time, item_id = datetime.fromisoformat(values[0]), uuid.UUID(values[1])
        if time.tzinfo is None:
            raise ValueError
        return time, item_id
    except Exception:
        raise AnalysisError(422, "INVALID_CURSOR", "分页标识无效") from None


async def history(db, user_id, limit, cursor):
    query = select(PromptAnalysis).where(
        PromptAnalysis.user_id == user_id,
        PromptAnalysis.deleted_at.is_(None),
        PromptAnalysis.history_retain_until > datetime.now(UTC),
        PromptAnalysis.conversation_id.is_(None),
    )
    if cursor:
        time, item_id = decode_cursor(cursor)
        query = query.where(
            or_(
                PromptAnalysis.created_at < time,
                and_(PromptAnalysis.created_at == time, PromptAnalysis.id < item_id),
            )
        )
    records = list(
        await db.scalars(
            query.order_by(PromptAnalysis.created_at.desc(), PromptAnalysis.id.desc()).limit(limit + 1)
        )
    )
    has_more = len(records) > limit
    selected = records[:limit]
    next_cursor = None
    if has_more:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps([last.created_at.isoformat(), str(last.id)]).encode()
        ).decode()
    return {
        "items": [await detail(db, job, summary=True) for job in selected],
        "pagination": {"limit": limit, "has_more": has_more, "next_cursor": next_cursor},
    }


async def edit(db, user_id, job_id, revision, prompt):
    await budget_lock(db)
    job = await owned_job(db, user_id, job_id, lock=True)
    if (
        job.status != "succeeded"
        or not job.generated_result
        or job.generated_result.get("schema_version") != "1"
    ):
        raise AnalysisError(409, "NOT_EDITABLE", "解析完成后才能编辑")
    if job.revision != revision:
        raise AnalysisError(409, "REVISION_CONFLICT", "记录已更新，请重新打开后编辑")
    job.edited_prompts = dict(prompt)
    job.revision += 1
    await db.commit()
    await db.refresh(job)
    return job


async def delete(db, user_id, job_id):
    job = await owned_job(db, user_id, job_id)
    await budget_lock(db)
    managed = job.conversation_id is not None or await db.scalar(
        select(exists().where(
            PromptAnalysis.user_id == user_id,
            PromptAnalysis.conversation_id == job.id,
        ))
    )
    if managed:
        raise AnalysisError(409, "CONVERSATION_MANAGED", "请从创作历史删除整段会话")
    asset = await db.scalar(
        select(PromptAnalysisAsset).where(PromptAnalysisAsset.id == job.asset_id).with_for_update()
    ) if job.asset_id is not None else None
    job = await owned_job(db, user_id, job_id, lock=True)
    now = datetime.now(UTC)
    job.deleted_at = now
    job.generated_result = None
    job.edited_prompts = None
    job.idea_text = None
    job.purpose = None
    job.style = None
    job.user_message = None
    job.context_snapshot = None
    if job.status == "queued":
        job.status, job.phase, job.quota_state = "canceled", "canceled", "released"
        job.finished_at = now
    await db.flush()
    if asset is not None:
        other = await db.scalar(
            select(
                exists().where(
                    PromptAnalysis.asset_id == asset.id,
                    PromptAnalysis.id != job.id,
                    PromptAnalysis.deleted_at.is_(None),
                    PromptAnalysis.image_retain_until > now,
                )
            )
        )
        if not other:
            asset.state, asset.purge_after = "deleting", now
    await db.commit()
