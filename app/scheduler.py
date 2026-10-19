"""
后台定时任务：浏览量批量回写数据库、热门搜索词排行聚合。

使用 APScheduler 的 AsyncIOScheduler，在 main.py 的应用生命周期中启动/关闭，
不额外起独立进程，适合 MVP 阶段单机部署的规模。
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import AsyncSessionLocal
from app.redis import get_redis_client
from app.services import search_service, submission_cleanup, traffic_service, view_count_service

logger = logging.getLogger(__name__)


async def _flush_view_counts_job() -> None:
    """每 5 分钟把 Redis 里累积的浏览量增量回写数据库，对应缓存策略表设计。"""
    async with AsyncSessionLocal() as db:
        redis_client = get_redis_client()
        flushed = await view_count_service.flush_view_counts(redis_client, db)
        if flushed:
            logger.info("浏览量回写完成，共更新 %d 条", flushed)


async def _refresh_trending_job() -> None:
    """每小时重新聚合热门搜索词排行，写入 Redis 缓存。"""
    async with AsyncSessionLocal() as db:
        redis_client = get_redis_client()
        items = await search_service.refresh_trending_cache(db, redis_client)
        logger.info("热门搜索词排行已刷新，共 %d 条", len(items))


async def _submission_cleanup_job():
    async with AsyncSessionLocal() as db:
        await submission_cleanup.cleanup_assets(db)


async def _traffic_cleanup_job():
    async with AsyncSessionLocal() as db:
        await traffic_service.cleanup_visits(db)


def create_scheduler() -> AsyncIOScheduler:
    """创建并配置好所有定时任务的调度器实例，由 main.py 负责启动和关闭。"""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_flush_view_counts_job, "interval", minutes=5, id="flush_view_counts")
    scheduler.add_job(_refresh_trending_job, "interval", hours=1, id="refresh_trending")
    scheduler.add_job(_submission_cleanup_job, "interval", minutes=30, id="submission_cleanup")
    scheduler.add_job(_traffic_cleanup_job, "interval", hours=24, id="traffic_cleanup")
    return scheduler
