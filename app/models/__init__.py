"""
统一导出所有 ORM 模型，供 alembic autogenerate 和应用代码 import。

新增模型时必须在此文件补充 import，否则 alembic 不会识别到新表。
"""

from app.models.content_report import ContentReport
from app.models.crawl_draft import CrawlDraft, CrawlDraftImage
from app.models.crawl_schedule import CrawlSchedule
from app.models.crawl_task import CrawlTask
from app.models.interaction import UserCollect, UserLike
from app.models.prompt import Prompt, PromptImage
from app.models.search_log import SearchLog
from app.models.user import User

__all__ = [
    "ContentReport",
    "CrawlDraft",
    "CrawlDraftImage",
    "CrawlTask",
    "CrawlSchedule",
    "Prompt",
    "PromptImage",
    "SearchLog",
    "User",
    "UserCollect",
    "UserLike",
]
