"""
采集脚本的数据库写入层：把抓到的素材写入 crawl_drafts 表。

**为什么这里直接用 SQLAlchemy 异步会话，而不是调用后端的 HTTP 接口？**
采集脚本和后端服务是同一个代码库（共享 app/models 的 ORM 定义），直接用 ORM
写库比调 HTTP 接口更直接：不需要后端服务处于运行状态、不需要处理管理员密钥、
不用为"脚本专用的写入接口"多维护一套 API。代价是脚本必须在本机、能连到数据库
才能跑——这对个人运营者手动触发的离线脚本完全够用。

如果要改成"脚本跑在另一台机器、只通过 API 写库"，需要额外开放一个采集写入接口，
当前阶段没必要，等真的有这个需求再说。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CrawlDraft, CrawlDraftImage


async def draft_exists(db: AsyncSession, source_platform: str, source_note_id: str) -> bool:
    """
    判断某条来源笔记是否已经采集过。

    用 (source_platform, source_note_id) 去重，避免同一个笔记被重复采集写入，
    这是脚本层的第一道防护（数据库层没有加唯一约束，因为同一条笔记理论上可能
    多次采集用于观察内容更新，交给脚本自己决定是否跳过）。
    """
    query = select(CrawlDraft.id).where(
        CrawlDraft.source_platform == source_platform,
        CrawlDraft.source_note_id == source_note_id,
    )
    return (await db.execute(query)).scalar_one_or_none() is not None


async def save_draft(
    db: AsyncSession,
    *,
    source_platform: str,
    source_note_id: str,
    source_url: str,
    raw_title: str | None,
    raw_content: str,
    author_name: str | None,
    local_image_paths: list[str],
) -> CrawlDraft:
    """
    写入一条采集草稿（含配图）。

    状态固定为 pending（待人工审核），不会自动转为正式内容——这是方案文档
    3.3 节确定的设计：脚本只负责抓素材，入库前必须有人工审核环节。
    """
    draft = CrawlDraft(
        source_platform=source_platform,
        source_note_id=source_note_id,
        source_url=source_url,
        raw_title=raw_title,
        raw_content=raw_content,
        author_name=author_name,
        status="pending",
    )
    db.add(draft)
    await db.flush()  # 先拿到 draft.id 供下面配图外键引用

    for index, path in enumerate(local_image_paths):
        db.add(CrawlDraftImage(draft_id=draft.id, local_path=path, sort_order=index))

    await db.commit()
    await db.refresh(draft)
    await db.refresh(draft, attribute_names=["images"])
    return draft
