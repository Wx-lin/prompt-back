"""Category-only migration with snapshot guards and transactional rollback journals."""

import re
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.models import CategoryLegacyMembership, CategoryMigrationChange, CrawlDraft, Prompt
from app.utils.category_catalog import CATEGORY_LABELS, LEGACY_CATEGORY_KEYS

_FIELDS = {
    "prompt": (
        "title",
        "prompt_text",
        "description",
        "category",
        "is_published",
        "review_status",
        "review_note",
        "reviewed_at",
        "models",
        "author_name",
        "author_avatar_url",
        "source_url",
        "submitter_id",
        "homepage_rank",
    ),
    "draft": (
        "raw_title",
        "raw_content",
        "extracted_prompt_text",
        "extracted_category",
        "extracted_models",
        "source_platform",
        "source_note_id",
        "source_url",
        "author_name",
        "author_avatar_url",
        "status",
        "review_note",
        "reviewed_at",
        "approved_prompt_id",
    ),
}
_MODELS = {"prompt": Prompt, "draft": CrawlDraft}
_CATEGORY_FIELDS = {"prompt": "category", "draft": "extracted_category"}
_IMAGE_FIELDS = {
    "prompt": ("id", "image_url", "thumbnail_url", "sort_order"),
    "draft": ("id", "local_path", "sort_order"),
}


def _value(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def record_state(entity, record, images):
    """Guard editable content/state/images; updated_at also changes on interactions."""

    def get(row, key):
        value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if key in ("updated_at", "reviewed_at") and isinstance(value, str):
            value = datetime.fromisoformat(value)
        return _value(value)

    state = {key: get(record, key) for key in _FIELDS[entity]}
    state["images"] = sorted(
        [{key: get(image, key) for key in _IMAGE_FIELDS[entity]} for image in images],
        key=lambda image: str(image["id"]),
    )
    return state


def _public(record):
    return record.get("is_published") is True and record.get("review_status") in (None, "approved")


def _validate_review(row, sources, seen):
    if not isinstance(row, dict) or row.get("entity") not in _MODELS:
        raise ValueError("Invalid review entity")
    record_id = str(uuid.UUID(row["id"]))
    key = (row["entity"], record_id)
    if key in seen or key not in sources:
        raise ValueError("Duplicate or missing snapshot record")
    seen.add(key)
    if type(row.get("confirmed")) is not bool:
        raise ValueError("confirmed must be a JSON boolean")
    if any(
        field in row for field in ("is_published", "review_status", "status", "prompt_text", "description")
    ):
        raise ValueError(
            "Only category changes are supported; state/content actions require separate approval tooling"
        )
    category = row.get("new_category")
    if category is not None and category not in CATEGORY_LABELS:
        raise ValueError("Invalid new category")
    if row["confirmed"] and category is None:
        raise ValueError("Confirmed rows need a valid new category")
    record = sources[key]
    if row.get("old_category") != record.get(_CATEGORY_FIELDS[row["entity"]]):
        raise ValueError("Review old category differs from snapshot")
    return key, record


def prepare_plan(snapshot, reviews):
    """Pure offline preview. An AI candidate is never treated as human approval."""
    sources = {
        (entity, str(uuid.UUID(record["id"]))): record
        for entity, table in [("prompt", "prompts"), ("draft", "crawl_drafts")]
        for record in snapshot.get(table, [])
    }
    applicable, skipped, seen = [], [], set()
    known = set(CATEGORY_LABELS) | set(LEGACY_CATEGORY_KEYS)
    unknown = sorted(
        {record["category"] for record in snapshot.get("prompts", []) if record["category"] not in known}
    )
    for row in reviews:
        key, record = _validate_review(row, sources, seen)
        entity, record_id = key
        if not row["confirmed"]:
            skipped.append({"entity": entity, "id": record_id, "reason": "unconfirmed"})
            continue
        if (entity == "prompt" and record["category"] not in known) or (
            entity == "draft" and record["status"] != "pending"
        ):
            skipped.append({"entity": entity, "id": record_id, "reason": "unknown_key_or_inactive_draft"})
            continue
        table, foreign = (
            ("prompt_images", "prompt_id") if entity == "prompt" else ("crawl_draft_images", "draft_id")
        )
        images = [image for image in snapshot.get(table, []) if str(image[foreign]) == record_id]
        applicable.append(
            {
                "entity": entity,
                "id": record_id,
                "before_state": record_state(entity, record, images),
                "after_category": row["new_category"],
            }
        )
    return _final_plan(snapshot, reviews, applicable, skipped, unknown)


def _final_plan(snapshot, reviews, applicable, skipped, unknown):
    proposed = {row["id"]: row["after_category"] for row in applicable if row["entity"] == "prompt"}
    before = Counter(record["category"] for record in snapshot.get("prompts", []))
    after = Counter(proposed.get(record["id"], record["category"]) for record in snapshot.get("prompts", []))
    unresolved = sum(
        _public(record) and proposed.get(record["id"], record["category"]) not in CATEGORY_LABELS
        for record in snapshot.get("prompts", [])
    )
    legacy = {
        key: [record["id"] for record in snapshot.get("prompts", []) if record["category"] == key]
        for key in sorted(LEGACY_CATEGORY_KEYS)
    }
    return {
        "applicable": applicable,
        "skipped": skipped,
        "legacy_memberships": legacy,
        "summary": {
            "total_reviewed": len(reviews),
            "applicable": len(applicable),
            "unconfirmed": sum(row["reason"] == "unconfirmed" for row in skipped),
            "unknown_keys": unknown,
            "unresolved_public": unresolved,
            "catalog_switch_ready": unresolved == 0,
            "before_counts": dict(before),
            "after_counts": dict(after),
        },
    }


async def export_snapshot(db):
    """Export only classification records/images; no users, credentials or assets."""
    from sqlalchemy import inspect

    from app.models import CrawlDraftImage, PromptImage

    result = {}
    for table, model in [
        ("prompts", Prompt),
        ("prompt_images", PromptImage),
        ("crawl_drafts", CrawlDraft),
        ("crawl_draft_images", CrawlDraftImage),
    ]:
        records = (await db.scalars(select(model).order_by(model.id))).all()
        result[table] = [
            {column.key: _value(getattr(record, column.key)) for column in inspect(model).columns}
            for record in records
        ]
    return result


def _batch(batch_id):
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", batch_id):
        raise ValueError("Invalid batch ID")


async def _locked_record(db, entity, record_id):
    model = _MODELS[entity]
    return await db.scalar(
        select(model)
        .where(model.id == uuid.UUID(record_id))
        .options(selectinload(model.images))
        .with_for_update()
    )


async def _seed_legacy(db, memberships):
    for legacy_key, ids in memberships.items():
        if legacy_key not in LEGACY_CATEGORY_KEYS:
            raise ValueError("Unknown legacy category")
        for record_id in ids:
            prompt_id = uuid.UUID(record_id)
            if (
                await db.get(Prompt, prompt_id) is not None
                and await db.get(CategoryLegacyMembership, (legacy_key, prompt_id)) is None
            ):
                db.add(CategoryLegacyMembership(legacy_key=legacy_key, prompt_id=prompt_id))
                await db.flush()


async def _apply_row(db, row, batch_id):
    entity, record_id = row["entity"], uuid.UUID(row["id"])
    journal = await db.get(CategoryMigrationChange, (batch_id, entity, record_id))
    if journal is not None:
        return "already_applied" if journal.rolled_back_at is None else "already_rolled_back"
    record = await _locked_record(db, entity, str(record_id))
    if record is None or record_state(entity, record, record.images) != row["before_state"]:
        return "concurrent_skipped"
    field = _CATEGORY_FIELDS[entity]
    if getattr(record, field) == row["after_category"]:
        return "unchanged"
    setattr(record, field, row["after_category"])
    await db.flush()
    await db.refresh(record)
    db.add(
        CategoryMigrationChange(
            batch_id=batch_id,
            entity=entity,
            record_id=record_id,
            before_state=row["before_state"],
            after_state=record_state(entity, record, record.images),
        )
    )
    return "applied"


async def apply_plan(db, plan, batch_id):
    """One recoverable transaction. Journals and legacy IDs commit with changes."""
    _batch(batch_id)
    counts = Counter({"applied": 0, "concurrent_skipped": 0, "already_applied": 0, "unchanged": 0})
    async with db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(731041314)"))
        if plan["applicable"]:
            await _seed_legacy(db, plan["legacy_memberships"])
        for row in sorted(plan["applicable"], key=lambda item: (item["entity"], item["id"])):
            if row["entity"] not in _MODELS or row["after_category"] not in CATEGORY_LABELS:
                raise ValueError("Invalid migration row")
            counts[await _apply_row(db, row, batch_id)] += 1
    return {"batch_id": batch_id, **dict(counts)}


async def rollback_batch(db, batch_id):
    """Restore only categories when the full post-migration version still matches."""
    _batch(batch_id)
    counts = Counter({"restored": 0, "concurrent_skipped": 0, "already_rolled_back": 0})
    async with db.begin():
        await db.execute(text("SELECT pg_advisory_xact_lock(731041314)"))
        journals = (
            await db.scalars(
                select(CategoryMigrationChange)
                .where(CategoryMigrationChange.batch_id == batch_id)
                .order_by(CategoryMigrationChange.entity, CategoryMigrationChange.record_id)
                .with_for_update()
            )
        ).all()
        for journal in journals:
            if journal.rolled_back_at is not None:
                counts["already_rolled_back"] += 1
                continue
            record = await _locked_record(db, journal.entity, str(journal.record_id))
            if record is None or record_state(journal.entity, record, record.images) != journal.after_state:
                counts["concurrent_skipped"] += 1
                continue
            field = _CATEGORY_FIELDS[journal.entity]
            setattr(record, field, journal.before_state[field])
            journal.rolled_back_at = datetime.now(UTC)
            counts["restored"] += 1
    return {"batch_id": batch_id, **dict(counts)}
