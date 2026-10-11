"""
采集任务 ORM 模型：记录后台"点击采集按钮"触发的每一次 MediaCrawler 子进程运行。

设计意图：采集是一个耗时几十秒到几分钟的异步过程（起浏览器、搜索、逐篇抓详情），
不能让 HTTP 请求同步等它跑完。所以后端收到"开始采集"请求后立即创建一条任务记录
（status=running）、用子进程方式拉起 MediaCrawler，自己马上返回；前端拿着
task.id 轮询这张表看进度，完成后再去看 crawl_drafts 里新增了哪些草稿。

多平台支持：一次"开始采集"如果勾选了多个平台（如小红书+微博），会创建多条
CrawlTask 记录（一个平台一条），通过 batch_id 关联成一组，按顺序依次执行
（见 app/services/crawl_task_service.py 的 start_crawl_batch），而不是单条记录
里塞多个平台——这样每个平台的进度/失败原因可以独立展示，互不影响。

这张表只记录"采集任务的执行情况"，不记录采集到的具体内容——内容本身仍然进
crawl_drafts 表，职责分开。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CrawlTask(Base):
    """
    一次采集任务的执行记录（一个任务对应一个平台）。

    status 流转：pending（排队等待前面的平台跑完）→ running（子进程正在跑）
    → success（采集完成且已导入草稿表）/ failed（子进程异常退出或导入失败，
    详见 error_message）。
    """

    __tablename__ = "crawl_tasks"

    __table_args__ = (
        UniqueConstraint("schedule_id", "scheduled_for", "source_platform", name="uq_crawl_schedule_run"),
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crawl_schedules.id", ondelete="SET NULL"), index=True
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_type: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    published_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    held_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stage: Mapped[str | None] = mapped_column(String(50))

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), index=True, comment="同一次点击「开始采集」创建的多个平台任务共享这个 ID"
    )

    source_platform: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="来源平台：xiaohongshu / weibo / kuaishou"
    )
    keyword: Mapped[str] = mapped_column(String(200), nullable=False, comment="搜索关键词")
    limit_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="本次期望采集的笔记数上限")

    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, comment="pending / running / success / failed"
    )
    # 采集完成后，本次新增写入 crawl_drafts 的条数（已存在的笔记会被跳过，不计入这个数字）
    new_draft_count: Mapped[int] = mapped_column(Integer, default=0)

    log_tail: Mapped[str | None] = mapped_column(Text, comment="子进程日志的尾部片段，用于失败时排查原因")
    error_message: Mapped[str | None] = mapped_column(Text, comment="失败时的简要错误说明")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="任务结束时间（成功或失败）"
    )
