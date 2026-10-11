"""
Redis 连接模块。

用于：
- 邮箱验证码存储（带 TTL 自动过期）
- 验证码发送频率限流
- 热门搜索词排行缓存
- 列表/详情查询缓存
"""

from functools import lru_cache

import redis.asyncio as redis

from app.config import get_settings


@lru_cache
def get_redis_pool() -> redis.ConnectionPool:
    """全局唯一的 Redis 连接池（进程内复用，不要每次请求都新建连接）。"""
    settings = get_settings()
    return redis.ConnectionPool.from_url(settings.redis_url, decode_responses=True)


def get_redis_client() -> redis.Redis:
    """
    获取 Redis 客户端实例。

    用法（FastAPI 依赖注入）：
        @router.get("/xxx")
        async def handler(r: redis.Redis = Depends(get_redis_client)):
            await r.get("key")
    """
    return redis.Redis(connection_pool=get_redis_pool())
