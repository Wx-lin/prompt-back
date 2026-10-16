"""SQL-only aggregates and bounded user pages, each from one statement snapshot."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrawlDraft, Prompt, PromptImage, User
from app.utils.category_catalog import CATEGORY_CATALOG, CATEGORY_LABELS
from app.utils.public_prompts import public_prompt_condition


def utc_now() -> datetime:
    return datetime.now(UTC)


def _count(model, *conditions):
    return select(func.count()).select_from(model).where(*conditions).scalar_subquery()


async def get_dashboard(db: AsyncSession) -> dict:
    now = utc_now()
    start = now.astimezone(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    public = public_prompt_condition()
    pending = ~public & (Prompt.review_status == "pending")
    rejected = ~public & (Prompt.review_status == "rejected")
    categories = (
        select(
            Prompt.category.label("category"),
            func.count().label("category_total"),
            func.count().filter(public).label("category_published"),
        )
        .group_by(Prompt.category)
        .subquery()
    )
    metrics = select(
        _count(User).label("users_total"),
        _count(User, User.is_active.is_(True)).label("users_enabled"),
        _count(User, User.is_active.is_(False)).label("users_disabled"),
        _count(User, User.created_at >= start, User.created_at < end).label("users_today"),
        _count(Prompt).label("content_total"),
        _count(Prompt, public).label("published"),
        _count(Prompt, pending).label("pending"),
        _count(Prompt, rejected).label("rejected"),
        _count(PromptImage).label("images"),
        _count(CrawlDraft).label("crawl_total"),
        _count(CrawlDraft, CrawlDraft.status == "pending").label("crawl_pending"),
    ).subquery()
    rows = (
        (await db.execute(select(metrics, categories).select_from(metrics.outerjoin(categories, true()))))
        .mappings()
        .all()
    )
    counts = rows[0]
    category_rows = {row["category"]: row for row in rows if row["category"] is not None}
    canonical = [
        dict(
            key=row["key"],
            label=row["label"],
            total=category_rows.get(row["key"], {}).get("category_total", 0),
            published=category_rows.get(row["key"], {}).get("category_published", 0),
        )
        for row in CATEGORY_CATALOG
    ]
    unknown = [row for key, row in category_rows.items() if key not in CATEGORY_LABELS]
    other = (
        [
            dict(
                key="__other__",
                label="其他/历史分类",
                total=sum(row["category_total"] for row in unknown),
                published=sum(row["category_published"] for row in unknown),
            )
        ]
        if unknown
        else []
    )
    return dict(
        generated_at=now,
        users=dict(
            total=counts["users_total"],
            enabled=counts["users_enabled"],
            disabled=counts["users_disabled"],
            today_new=counts["users_today"],
        ),
        content=dict(
            total=counts["content_total"],
            published=counts["published"],
            pending=counts["pending"],
            rejected=counts["rejected"],
            other_unpublished=counts["content_total"]
            - counts["published"]
            - counts["pending"]
            - counts["rejected"],
            images=counts["images"],
        ),
        crawl=dict(total=counts["crawl_total"], pending=counts["crawl_pending"]),
        categories=canonical + other,
    )


async def list_users(
    db: AsyncSession, search: str, is_active: bool | None, page: int, page_size: int
) -> dict:
    term = search.strip()
    # Escape the LIKE escape character first; user wildcards are ordinary text.
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conditions = (
        [or_(User.nickname.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\"))]
        if term
        else []
    )
    filters = conditions + ([User.is_active.is_(is_active)] if is_active is not None else [])
    fields = (User.id, User.nickname, User.email, User.is_active, User.created_at, User.last_login_at)
    entries = (
        select(*fields)
        .where(*filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .subquery()
    )
    totals = select(_count(User, *filters).label("total"), _count(User).label("all_users_total")).subquery()
    statement = (
        select(totals, entries)
        .select_from(totals.outerjoin(entries, true()))
        .order_by(entries.c.created_at.desc(), entries.c.id.desc())
    )
    rows = (await db.execute(statement)).mappings().all()
    return dict(
        items=[{field.key: row[field.key] for field in fields} for row in rows if row["id"] is not None],
        total=rows[0]["total"],
        all_users_total=rows[0]["all_users_total"],
        page=page,
        page_size=page_size,
    )
