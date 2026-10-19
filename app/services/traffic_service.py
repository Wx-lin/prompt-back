"""Exact daily/window unique browsers using a single SQL snapshot."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, delete, distinct, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.visitor_daily import TrafficTrackingState, VisitorDaily

BEIJING = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(UTC)


async def record_visit(db: AsyncSession, visitor_hash: str) -> None:
    now = utc_now()
    await db.execute(insert(TrafficTrackingState).values(id=1, enabled_at=now).on_conflict_do_nothing())
    await db.execute(
        insert(VisitorDaily)
        .values(
            date=now.astimezone(BEIJING).date(),
            visitor_hash=visitor_hash,
        )
        .on_conflict_do_nothing()
    )
    await db.commit()


async def get_traffic(db: AsyncSession) -> dict:
    now = utc_now()
    today = now.astimezone(BEIJING).date()
    start = today - timedelta(days=29)
    # One statement gives all counts, state and daily rows the same MVCC snapshot.
    recent = select(VisitorDaily).where(VisitorDaily.date.between(start, today)).cte("recent")
    daily = select(recent.c.date, func.count().label("uv")).group_by(recent.c.date).cte("daily")
    statement = select(
        select(TrafficTrackingState.enabled_at).where(TrafficTrackingState.id == 1).scalar_subquery(),
        select(func.count()).select_from(recent).where(recent.c.date == today).scalar_subquery(),
        select(func.count(distinct(recent.c.visitor_hash)))
        .where(recent.c.date >= today - timedelta(days=6))
        .scalar_subquery(),
        select(func.count(distinct(recent.c.visitor_hash))).scalar_subquery(),
        select(func.json_object_agg(func.cast(daily.c.date, String), daily.c.uv))
        .select_from(daily)
        .scalar_subquery(),
    )
    since, today_uv, seven_uv, thirty_uv, days = (await db.execute(statement)).one()
    days = days or {}
    return {
        "generated_at": now,
        "tracking_since": since,
        "timezone": "Asia/Shanghai",
        "today_uv": today_uv,
        "seven_day_uv": seven_uv,
        "thirty_day_uv": thirty_uv,
        "daily": [
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "uv": days.get((start + timedelta(days=i)).isoformat(), 0),
            }
            for i in range(30)
        ],
    }


async def cleanup_visits(db: AsyncSession) -> int:
    cutoff = utc_now().astimezone(BEIJING).date() - timedelta(days=89)
    result = await db.execute(delete(VisitorDaily).where(VisitorDaily.date < cutoff))
    await db.commit()
    return result.rowcount
