"""
分类相关常量和路由：获取分类列表（含每个分类的内容数量）。

对应 PRD 3.3、后端技术选型.md 接口清单 GET /api/categories。
分类枚举值在此集中定义一处，避免分散在各个文件里手写字符串导致不一致。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Prompt

router = APIRouter(prefix="/api/categories", tags=["categories"])

# 分类枚举值与展示名对应表。
# 2026-10-09 重新设计：按"创作意图"分类（而非题材内容），参考 Civitai 的标签式
# 分类思路，而非 PromptBase 的"先分生成工具"思路——本产品是"浏览发现+复制使用"
# 场景，不是"按模型筛选购买"的交易场景，按创作意图分更符合国内用户找灵感的习惯。
# 原七分类（head_avatar/portrait/poster/illustration/logo/food/scene）已废弃，
# 废弃原因：维度混杂（"头像/壁纸"把用途和题材混在一起）、粒度不一致、分类重叠
# （poster 和 illustration 经常难以取舍）。
CATEGORY_LABELS: dict[str, str] = {
    "portrait": "人像写真",
    "avatar": "头像 & IP",
    "poster": "海报 & 封面",
    "illustration": "插画 & 概念艺术",
    "photography": "模拟摄影",
    "character": "角色 & 人物设计",
    "image_edit": "图片编辑",
    "workflow": "工作流 & 技巧",
}


@router.get("")
async def list_categories(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """返回分类列表及每个分类下已发布的内容数量，用于首页分类标签展示计数。"""
    query = (
        select(Prompt.category, func.count().label("cnt"))
        .where(Prompt.is_published.is_(True))
        .group_by(Prompt.category)
    )
    rows = (await db.execute(query)).all()
    count_map = {category: cnt for category, cnt in rows}

    return [
        {"key": key, "label": label, "count": count_map.get(key, 0)}
        for key, label in CATEGORY_LABELS.items()
    ]
