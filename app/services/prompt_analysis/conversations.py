"""Conversation views over durable analysis turns; no duplicate result storage."""

import base64
import copy
import json
from datetime import UTC, datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from app.models import PromptAnalysis, PromptAnalysisAsset
from app.schemas.prompt_analysis import BilingualPrompt
from app.services.prompt_analysis import api, assets
from app.services.prompt_analysis.errors import AnalysisError

MAX_TURNS = 100


def group_id(job):
    return job.conversation_id or job.id


def title(job):
    result = job.generated_result or {}
    return (result.get("title") or job.user_message or job.idea_text or "图片创作")[:80]


def visible(user_id):
    return (
        PromptAnalysis.user_id == user_id,
        PromptAnalysis.deleted_at.is_(None),
        PromptAnalysis.history_retain_until > datetime.now(UTC),
    )


async def owned_root(db, user_id, conversation_id):
    root = await api.owned_job(db, user_id, conversation_id)
    if group_id(root) != root.id:
        raise assets.not_found()
    return root


def membership(conversation_id):
    return or_(PromptAnalysis.conversation_id == conversation_id, PromptAnalysis.id == conversation_id)


async def latest_turn(db, user_id, conversation_id, *, retained=True):
    query = select(PromptAnalysis).where(PromptAnalysis.user_id == user_id, membership(conversation_id))
    if retained:
        query = query.where(*visible(user_id))
    return await db.scalar(
        query.order_by(
            func.coalesce(PromptAnalysis.conversation_turn, 1).desc(),
            PromptAnalysis.created_at.desc(),
            PromptAnalysis.id.desc(),
        ).limit(1)
    )


async def prepare(db, user_id, conversation_id, parent_id, parent_revision, *, retry_id=None):
    """Called under shared budget lock after asset locks and active-work check."""
    if conversation_id is None:
        return {"conversation_turn": 1, "context_snapshot": None, "parent_analysis_id": None}
    root = await owned_root(db, user_id, conversation_id)
    latest = await latest_turn(db, user_id, conversation_id)
    count = await db.scalar(
        select(func.count())
        .select_from(PromptAnalysis)
        .where(
            PromptAnalysis.user_id == user_id,
            membership(conversation_id),
        )
    )
    if count >= MAX_TURNS:
        raise AnalysisError(429, "CONVERSATION_LIMIT", "本次创作已达上限，请新建创作")
    if retry_id is not None:
        return {
            **_retry_context(latest, retry_id, count + 1),
            "history_retain_until": root.history_retain_until,
        }
    if latest is None or latest.id != parent_id or latest.status != "succeeded":
        raise AnalysisError(409, "CONVERSATION_CHANGED", "对话已更新，请重新打开后继续")
    if latest.revision != parent_revision:
        raise AnalysisError(409, "PARENT_REVISION_CONFLICT", "上一版已修改，请刷新后继续")
    context = await _creation_context(db, user_id, conversation_id)
    return {
        "conversation_turn": count + 1,
        "context_snapshot": context,
        "parent_analysis_id": latest.id,
        "history_retain_until": root.history_retain_until,
    }


async def _creation_context(db, user_id, conversation_id):
    # The latest message anchors concurrency. The latest actual prompt anchors
    # creative revisions, even after many intervening ordinary replies.
    prompt_job = await db.scalar(select(PromptAnalysis).where(
        *visible(user_id), membership(conversation_id),
        PromptAnalysis.status == "succeeded",
        PromptAnalysis.generated_result["schema_version"].astext == "1",
    ).order_by(func.coalesce(PromptAnalysis.conversation_turn, 1).desc()).limit(1))
    recent = list(
        await db.scalars(
            select(PromptAnalysis)
            .where(
                *visible(user_id),
                membership(conversation_id),
                PromptAnalysis.status == "succeeded",
            )
            .order_by(
                func.coalesce(PromptAnalysis.conversation_turn, 1).desc(),
                PromptAnalysis.created_at.desc(),
                PromptAnalysis.id.desc(),
            )
            .limit(6)
        )
    )
    prompt = BilingualPrompt.model_validate(
        prompt_job.edited_prompts or prompt_job.generated_result["prompt"]
    ) if prompt_job else None
    return {
        "prompt": prompt.model_dump() if prompt else None,
        "title": title(prompt_job or recent[0]),
        "breakdown": copy.deepcopy(prompt_job.generated_result["breakdown"]) if prompt_job else None,
        "source_type": prompt_job.input_type if prompt_job else None,
        "recent_messages": [item.user_message or item.idea_text or "" for item in reversed(recent)],
        "recent_dialogue": [
            {
                "user": item.user_message or item.idea_text or "",
                "assistant": (item.generated_result or {}).get("reply", ""),
            }
            for item in reversed(recent)
        ],
    }


def _retry_context(latest, retry_id, version):
    if latest is None or latest.id != retry_id:
        raise AnalysisError(409, "CONVERSATION_CHANGED", "对话已更新，请重新打开后重试")
    if latest.status == "unknown":
        raise AnalysisError(409, "CONVERSATION_UNKNOWN", "上次生成状态待确认，请稍后从历史查看")
    if latest.status not in {"failed", "canceled"}:
        raise AnalysisError(409, "NOT_RETRYABLE", "当前记录不能重试")
    return {
        "conversation_turn": version,
        "context_snapshot": copy.deepcopy(latest.context_snapshot),
        "parent_analysis_id": latest.parent_analysis_id,
    }


async def create(db, redis, user_id, body, key, source, *, conversation_id=None):
    return await api.create(
        db,
        redis,
        user_id,
        body.asset_id,
        key,
        source,
        input_type="image" if body.asset_id else "text",
        idea_text=body.message if not body.asset_id else None,
        chat=True,
        conversation_id=conversation_id,
        parent_analysis_id=getattr(body, "parent_analysis_id", None),
        parent_revision=getattr(body, "parent_revision", None),
        user_message=body.message or "",
        conversation_mode=body.mode,
    )


async def retry(db, redis, user_id, conversation_id, analysis_id, key, source):
    await owned_root(db, user_id, conversation_id)
    job = await api.owned_job(db, user_id, analysis_id)
    if group_id(job) != conversation_id:
        raise assets.not_found()
    return await api.create(
        db,
        redis,
        user_id,
        job.asset_id,
        key,
        source,
        input_type=job.input_type,
        idea_text=job.idea_text,
        purpose=job.purpose,
        style=job.style,
        chat=True,
        conversation_id=conversation_id,
        user_message=job.user_message or job.idea_text or "",
        retry_analysis_id=analysis_id,
        conversation_mode=job.config_snapshot.get("conversation_mode", "auto"),
    )


async def detail(db, user_id, conversation_id):
    root = await owned_root(db, user_id, conversation_id)
    turns = list(
        await db.scalars(
            select(PromptAnalysis)
            .where(
                *visible(user_id),
                membership(conversation_id),
            )
            .order_by(
                func.coalesce(PromptAnalysis.conversation_turn, 1),
                PromptAnalysis.created_at,
                PromptAnalysis.id,
            )
            .limit(MAX_TURNS)
        )
    )
    if not turns:
        raise assets.not_found()
    values, number = [], 0
    for turn in turns:
        is_prompt = (
            turn.status == "succeeded"
            and (turn.generated_result or {}).get("schema_version") == "1"
        )
        if is_prompt:
            number += 1
        values.append(await api.detail(db, turn, prompt_number=number if is_prompt else None))
    return {
        "id": str(root.id),
        "title": title(root),
        "created_at": root.created_at.isoformat(),
        "updated_at": max(turn.updated_at for turn in turns).isoformat(),
        "turns": values,
    }


async def history(db, user_id, limit, cursor):
    group = func.coalesce(PromptAnalysis.conversation_id, PromptAnalysis.id)
    ranked = (
        select(
            PromptAnalysis.id.label("analysis_id"),
            group.label("conversation_id"),
            func.row_number()
            .over(
                partition_by=group,
                order_by=(
                    func.coalesce(PromptAnalysis.conversation_turn, 1).desc(),
                    PromptAnalysis.created_at.desc(),
                    PromptAnalysis.id.desc(),
                ),
            )
            .label("position"),
            func.max(PromptAnalysis.updated_at).over(partition_by=group).label("activity"),
        )
        .where(*visible(user_id))
        .subquery()
    )
    root = aliased(PromptAnalysis)
    query = (
        select(PromptAnalysis, root, ranked.c.activity)
        .join(
            ranked,
            ranked.c.analysis_id == PromptAnalysis.id,
        )
        .join(root, root.id == ranked.c.conversation_id)
        .where(
            ranked.c.position == 1,
            root.user_id == user_id,
            root.deleted_at.is_(None),
            root.history_retain_until > datetime.now(UTC),
        )
    )
    if cursor:
        time, item_id = api.decode_cursor(cursor)
        query = query.where(
            or_(
                ranked.c.activity < time,
                and_(
                    ranked.c.activity == time,
                    ranked.c.conversation_id < item_id,
                ),
            )
        )
    rows = (
        await db.execute(
            query.order_by(
                ranked.c.activity.desc(),
                ranked.c.conversation_id.desc(),
            ).limit(limit + 1)
        )
    ).all()
    selected, has_more = rows[:limit], len(rows) > limit
    next_cursor = None
    if has_more:
        _, last_root, activity = selected[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                [activity.isoformat(), str(last_root.id)],
            ).encode()
        ).decode()
    return {
        "items": [
            {
                "id": str(root.id),
                "title": title(root),
                "status": latest.status,
                "created_at": root.created_at.isoformat(),
                "updated_at": activity.isoformat(),
                "latest_analysis_id": str(latest.id),
                "version": latest.conversation_turn or 1,
            }
            for latest, root, activity in selected
        ],
        "pagination": {"limit": limit, "has_more": has_more, "next_cursor": next_cursor},
    }


async def delete(db, user_id, conversation_id):
    await api.budget_lock(db)
    await owned_root(db, user_id, conversation_id)
    ids = list(
        await db.scalars(
            select(PromptAnalysis.asset_id)
            .where(
                PromptAnalysis.user_id == user_id,
                membership(conversation_id),
                PromptAnalysis.asset_id.is_not(None),
            )
            .distinct()
            .order_by(PromptAnalysis.asset_id)
        )
    )
    images = (
        list(
            await db.scalars(
                select(PromptAnalysisAsset)
                .where(
                    PromptAnalysisAsset.id.in_(ids),
                )
                .order_by(PromptAnalysisAsset.id)
                .with_for_update()
            )
        )
        if ids
        else []
    )
    jobs = list(
        await db.scalars(
            select(PromptAnalysis)
            .where(
                PromptAnalysis.user_id == user_id,
                membership(conversation_id),
            )
            .order_by(PromptAnalysis.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    now = datetime.now(UTC)
    for job in jobs:
        job.deleted_at = now
        job.generated_result = job.edited_prompts = job.context_snapshot = None
        job.idea_text = job.user_message = job.purpose = job.style = None
        if job.status == "queued":
            job.status, job.phase, job.quota_state = "canceled", "canceled", "released"
            job.finished_at = now
    await db.flush()
    for image in images:
        if not await db.scalar(
            select(
                exists().where(
                    PromptAnalysis.asset_id == image.id,
                    PromptAnalysis.deleted_at.is_(None),
                    PromptAnalysis.image_retain_until > now,
                )
            )
        ):
            image.state, image.purge_after = "deleting", now
    await db.commit()
