"""Persist exports without counting dimension totals more than once."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.seo import SeoSnapshot


def metrics(rows):
    clicks = sum(row["clicks"] for row in rows)
    impressions = sum(row["impressions"] for row in rows)
    measured = [row for row in rows if row["impressions"]]
    position = None
    if measured and all(row.get("position") is not None for row in measured):
        position = sum(row["position"] * row["impressions"] for row in measured) / impressions
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0,
        "position": position,
    }


async def import_snapshot(db, payload):
    # Serialize imports per engine/dimension so concurrent overlapping writes cannot race.
    lock = (1 if payload.engine == "google" else 2) * 10 + {"daily": 1, "query": 2, "page": 3}[
        payload.dimension
    ]
    await db.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
    overlaps = (
        await db.scalars(
            select(SeoSnapshot).where(
                SeoSnapshot.engine == payload.engine,
                SeoSnapshot.dimension == payload.dimension,
                SeoSnapshot.start_date <= payload.end_date,
                SeoSnapshot.end_date >= payload.start_date,
            )
        )
    ).all()
    exact = next(
        (s for s in overlaps if s.start_date == payload.start_date and s.end_date == payload.end_date), None
    )
    if any(s is not exact for s in overlaps):
        raise HTTPException(409, "该维度已有重叠日期的数据，请使用相同日期窗口重新导入")
    values = dict(
        engine=payload.engine,
        dimension=payload.dimension,
        start_date=payload.start_date,
        end_date=payload.end_date,
        rows=[row.model_dump() for row in payload.rows],
        updated_at=datetime.now(UTC),
    )
    if exact:
        await db.delete(exact)
        await db.flush()
    db.add(SeoSnapshot(**values))
    await db.commit()
    return {"imported": len(payload.rows), "engine": payload.engine, "dimension": payload.dimension}


async def get_report(db, engine, start, end):
    snapshots = (await db.scalars(select(SeoSnapshot).where(SeoSnapshot.engine == engine))).all()
    coverage, result = {}, {}
    for dimension, output in [("daily", "daily"), ("query", "queries"), ("page", "pages")]:
        selected = [
            s
            for s in snapshots
            if s.dimension == dimension
            and (
                (s.start_date <= end and s.end_date >= start)
                if dimension == "daily"
                else (s.start_date >= start and s.end_date <= end)
            )
        ]
        rows = [
            row
            for s in selected
            for row in s.rows
            if dimension != "daily" or start.isoformat() <= row["key"] <= end.isoformat()
        ]
        grouped = {}
        for row in rows:
            grouped.setdefault(row["key"], []).append(row)
        merged = [{"key": key, **metrics(values)} for key, values in grouped.items()]
        result[output] = sorted(
            merged,
            key=(lambda r: r["key"])
            if dimension == "daily"
            else (lambda r: (-r["clicks"], -r["impressions"], r["key"])),
        )
        covered = set()
        for s in selected:
            day = max(start, s.start_date)
            while day <= min(end, s.end_date):
                covered.add(day.isoformat())
                day += timedelta(days=1)
        if dimension == "daily":
            covered = set(grouped)
        coverage[dimension] = {
            "status": "missing"
            if not selected
            else "complete"
            if len(covered) == (end - start).days + 1
            else "partial",
            "start_date": min(covered) if covered else None,
            "end_date": max(covered) if covered else None,
            "row_count": len(merged),
        }
    return {
        "engine": engine,
        "start_date": start,
        "end_date": end,
        "connection_status": "manual_import" if snapshots else "not_connected",
        "source_timezone": "America/Los_Angeles" if engine == "google" else "source_report_timezone",
        "totals": metrics(result["daily"]) if result["daily"] else None,
        **result,
        "coverage": coverage,
        "updated_at": max(s.updated_at for s in snapshots) if snapshots else None,
    }
