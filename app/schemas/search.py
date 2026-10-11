"""
搜索相关 Pydantic Schema。
"""

from pydantic import BaseModel


class TrendItem(BaseModel):
    """单个热门搜索词，trend 字段驱动前端箭头图标（up/down/flat）。"""

    keyword: str
    rank: int
    trend: str  # "up" | "down" | "flat"


class TrendingResponse(BaseModel):
    """热门搜索词列表响应，对应搜索弹窗的"热门搜索"区域。"""

    items: list[TrendItem]


class SearchLogRequest(BaseModel):
    """记录一次实际搜索行为，用于后续统计热门搜索词排行。"""

    keyword: str
