"""
浏览量统计服务：先写 Redis 计数，定时批量回写数据库，减少高频写请求对数据库的压力。

对应后端技术选型.md 第六节缓存策略："浏览量计数，批量写入，每5分钟刷入DB"。

MVP 阶段的简化实现：直接用 Redis INCR 累加增量，提供一个可被定时任务调用的
flush 函数做批量回写；定时任务本身（如 APScheduler 或系统 crontab）在
main.py 中注册，这里只提供可被调用的纯函数。
"""

import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Prompt

_VIEW_COUNT_KEY_PREFIX = "view_count_delta:"


async def record_view(redis_client: Redis, prompt_id: uuid.UUID) -> None:
    """记录一次浏览，只做 Redis 自增，不直接写数据库（避免每次浏览都触发一次 UPDATE）。"""
    await redis_client.incr(f"{_VIEW_COUNT_KEY_PREFIX}{prompt_id}")


async def flush_view_counts(redis_client: Redis, db: AsyncSession) -> int:
    """
    批量把 Redis 中累积的浏览量增量回写到数据库，由定时任务周期调用。

    返回本次成功回写的记录条数，便于日志观察任务是否正常工作。
    """
    keys = await redis_client.keys(f"{_VIEW_COUNT_KEY_PREFIX}*")
    flushed = 0
    for key in keys:
        delta_str = await redis_client.get(key)
        if not delta_str:
            continue
        delta = int(delta_str)
        prompt_id = uuid.UUID(key.removeprefix(_VIEW_COUNT_KEY_PREFIX))

        result = await db.execute(select(Prompt).where(Prompt.id == prompt_id))
        prompt = result.scalar_one_or_none()
        if prompt is not None:
            prompt.view_count += delta
            flushed += 1

        await redis_client.delete(key)

    await db.commit()
    return flushed
