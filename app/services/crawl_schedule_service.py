"""Schedule calculations and persistence."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.crawl_schedule import CrawlSchedule

SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_run(times: list[str], now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(SHANGHAI)
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        for clock in sorted(set(times)):
            hour, minute = map(int, clock.split(":"))
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI)
            if candidate > local:
                return candidate.astimezone(UTC)
    raise ValueError("schedule requires at least one time")


async def list_schedules(db):
    result = await db.scalars(select(CrawlSchedule).order_by(CrawlSchedule.created_at.desc()))
    return list(result)


async def save_schedule(db, payload, schedule=None):
    values = payload.model_dump(exclude_unset=schedule is not None)
    if any(value is None for value in values.values()):
        raise ValueError("设置不能为 null")
    if schedule is None:
        schedule = CrawlSchedule(**values)
        db.add(schedule)
    else:
        for key, value in values.items():
            setattr(schedule, key, value)
    schedule.next_run_at = next_run(schedule.times, datetime.now(UTC)) if schedule.enabled else None
    await db.commit()
    await db.refresh(schedule)
    return schedule
