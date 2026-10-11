"""
搜索相关路由：热门搜索词列表、记录一次搜索日志。

对应 PRD 搜索弹窗设计（最近搜索/热门搜索）、后端技术选型.md 3.3 节。
"最近搜索"不在此文件实现，由前端 localStorage 本地处理，详见
后端技术选型.md 3.3 节末尾说明。
"""

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.redis import get_redis_client
from app.schemas.search import SearchLogRequest, TrendingResponse
from app.services import search_service
from app.utils.deps import get_optional_current_user

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/trending", response_model=TrendingResponse)
async def get_trending(redis_client: Redis = Depends(get_redis_client)) -> TrendingResponse:
    """
    获取热门搜索词列表，供搜索弹窗打开时默认展示。

    数据来自定时任务离线聚合后写入的 Redis 缓存，本接口只读缓存，
    不做实时数据库聚合查询（避免拖慢搜索弹窗的打开速度）。
    """
    items = await search_service.get_trending(redis_client)
    return TrendingResponse(items=items)


@router.post("/log", status_code=204)
async def log_search(
    body: SearchLogRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
) -> None:
    """
    记录一次实际搜索行为（用户按 Enter 或点击搜索结果时前端调用），
    匿名也可调用，用于后续统计热门搜索词排行。
    """
    user_id = current_user.id if current_user else None
    await search_service.log_search(db, body.keyword, user_id)
