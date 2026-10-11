"""
搜索相关业务逻辑：记录搜索日志、聚合计算热门搜索词排行。

对应后端技术选型.md 3.3 节。热门排行的计算是离线聚合（定时任务跑一次），
不是每次请求都实时查数据库，避免高频接口拖慢搜索弹窗的打开速度。
"""

import json
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SearchLog

_TRENDING_CACHE_KEY = "search:trending"
_TRENDING_PREV_RANK_KEY = "search:trending:prev_rank"
_AGGREGATE_DAYS = 7
_TOP_N = 8


async def log_search(db: AsyncSession, keyword: str, user_id) -> None:
    """记录一次搜索行为，user_id 可为 None（匿名搜索也要统计进热门排行）。"""
    db.add(SearchLog(keyword=keyword.strip(), user_id=user_id))
    await db.commit()


async def refresh_trending_cache(db: AsyncSession, redis_client: Redis) -> list[dict]:
    """
    聚合最近 N 天的搜索日志，计算热门搜索词排行，写入 Redis 缓存。

    由定时任务周期调用（建议每小时一次），不在用户请求路径上执行聚合查询。
    trend 字段通过对比本次排名和上一次缓存的排名得出：排名数字变小→up，变大→down，不变→flat。
    """
    since = datetime.now(UTC) - timedelta(days=_AGGREGATE_DAYS)
    query = (
        select(SearchLog.keyword, func.count().label("cnt"))
        .where(SearchLog.created_at >= since)
        .group_by(SearchLog.keyword)
        .order_by(func.count().desc())
        .limit(_TOP_N)
    )
    rows = (await db.execute(query)).all()

    prev_rank_raw = await redis_client.get(_TRENDING_PREV_RANK_KEY)
    prev_rank: dict[str, int] = json.loads(prev_rank_raw) if prev_rank_raw else {}

    items = []
    current_rank: dict[str, int] = {}
    for rank, (keyword, _cnt) in enumerate(rows, start=1):
        current_rank[keyword] = rank
        old_rank = prev_rank.get(keyword)
        if old_rank is None:
            trend = "flat"
        elif rank < old_rank:
            trend = "up"
        elif rank > old_rank:
            trend = "down"
        else:
            trend = "flat"
        items.append({"keyword": keyword, "rank": rank, "trend": trend})

    await redis_client.set(_TRENDING_CACHE_KEY, json.dumps(items), ex=3600)
    await redis_client.set(_TRENDING_PREV_RANK_KEY, json.dumps(current_rank), ex=3600 * 2)
    return items


async def get_trending(redis_client: Redis) -> list[dict]:
    """读取缓存的热门搜索词列表，供 GET /api/search/trending 使用。"""
    cached = await redis_client.get(_TRENDING_CACHE_KEY)
    return json.loads(cached) if cached else []
