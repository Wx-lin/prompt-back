"""
后台管理路由：内容的增删改查、举报的查看与处理。

所有接口都挂了 Depends(verify_admin_key) 做权限校验（见 app/utils/admin_auth.py），
调用时需要在请求头带上 `X-Admin-Key: <ADMIN_SECRET_KEY>`。

这不是面向普通用户的接口，不会出现在 PRD 描述的用户可见功能里，对应 PRD 3.5
"内容管理（后台运营，非用户可见）"。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CrawlDraft, Prompt
from app.schemas.admin import (
    AdminPromptCreate,
    AdminPromptListResponse,
    AdminPromptOut,
    AdminPromptUpdate,
    AdminReportHandle,
    AdminReportListResponse,
    AdminReportOut,
)
from app.schemas.crawl import (
    CrawlDraftApprove,
    CrawlDraftImageOut,
    CrawlDraftListResponse,
    CrawlDraftOut,
    CrawlDraftRejectBody,
    CrawlDraftReviewUpdate,
)
from app.schemas.crawl_task import (
    CrawlPlatformInfo,
    CrawlPlatformListResponse,
    CrawlTaskListResponse,
    CrawlTaskOut,
    CrawlTaskStartRequest,
)
from app.services import admin_service, crawl_service, crawl_task_service
from app.services.submission_service import SubmissionConflict
from app.utils.admin_auth import verify_admin_key

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(verify_admin_key)],
)


# MediaCrawler 子进程落盘的媒体目录按平台代号命名（见 crawl_task_service.PLATFORM_SPECS
# 的 cli_code 字段：xhs / wb / ks），本地相对路径都形如 "{cli_code}/media/{content_id}/xxx.jpg"
_MEDIACRAWLER_PATH_PREFIXES = ("xhs/media/", "wb/media/", "ks/media/")


def _resolve_image_url(local_path: str) -> str:
    """
    把草稿图片的 local_path 转成可直接访问的 URL，兼容三种历史格式（见 _draft_to_out 的说明）。
    """
    if local_path.startswith("http"):
        return local_path
    if local_path.startswith(_MEDIACRAWLER_PATH_PREFIXES):
        return f"/mediacrawler-media/{local_path}"
    return f"/local-media/{local_path}"


def _draft_to_out(draft: CrawlDraft) -> CrawlDraftOut:
    """
    把 CrawlDraft ORM 对象转成响应结构，顺带把图片路径拼接成完整的可访问 URL。

    local_path 字段现在有三种可能的内容（历史原因，字段名没有改，含义扩展了）：
    1. 完整的远程 URL（早期小红书手写脚本直接存 CDN 地址）→ 原样返回
    2. app/config.py crawl_media_dir 下的本地相对路径（早期手写脚本自己下载的图片）
       → 拼接 /local-media/ 前缀
    3. MediaCrawler 的 data/ 目录下的本地相对路径（当前 crawl_task_service 统一走
       这条路径，三个平台都适用）→ 拼接 /mediacrawler-media/ 前缀
    用路径是否以 "{platform_code}/media/" 形式开头区分 2 和 3。
    """
    return CrawlDraftOut(
        id=draft.id,
        source_platform=draft.source_platform,
        source_note_id=draft.source_note_id,
        source_url=draft.source_url,
        raw_title=draft.raw_title,
        raw_content=draft.raw_content,
        author_name=draft.author_name,
        author_avatar_url=draft.author_avatar_url,
        extracted_prompt_text=draft.extracted_prompt_text,
        extracted_category=draft.extracted_category,
        extracted_models=draft.extracted_models,
        status=draft.status,
        approved_prompt_id=draft.approved_prompt_id,
        review_note=draft.review_note,
        created_at=draft.created_at,
        reviewed_at=draft.reviewed_at,
        images=[
            CrawlDraftImageOut(
                id=img.id, image_url=_resolve_image_url(img.local_path), sort_order=img.sort_order
            )
            for img in draft.images
        ],
    )


# ---------- 内容管理 ----------


@router.get("/prompts", response_model=AdminPromptListResponse)
async def list_prompts(
    is_published: bool | None = Query(None, description="筛选发布状态，不传则返回全部（含草稿）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AdminPromptListResponse:
    """
    后台内容列表，默认按创建时间倒序，可用 is_published=false 筛选出"待审核草稿"。
    """
    rows, total = await admin_service.list_prompts_for_admin(db, is_published, page, page_size)
    return AdminPromptListResponse(
        items=[AdminPromptOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/prompts/{prompt_id}", response_model=AdminPromptOut)
async def get_prompt(prompt_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AdminPromptOut:
    """后台查看单条内容详情（含草稿态），编辑前通常先调用这个拿到当前完整数据。"""
    prompt = await admin_service.get_prompt_for_admin(db, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="内容不存在")
    return AdminPromptOut.model_validate(prompt)


@router.post("/prompts", response_model=AdminPromptOut, status_code=201)
async def create_prompt(body: AdminPromptCreate, db: AsyncSession = Depends(get_db)) -> AdminPromptOut:
    """
    创建一条新内容，对应采集流程"人工审核确认后入库"这一步
    （见 docs/aigc-prompt-爬虫方案.md）。默认 is_published=False，先是草稿态。
    """
    prompt = await admin_service.create_prompt(db, body)
    return AdminPromptOut.model_validate(prompt)


@router.patch("/prompts/{prompt_id}", response_model=AdminPromptOut)
async def update_prompt(
    prompt_id: uuid.UUID, body: AdminPromptUpdate, db: AsyncSession = Depends(get_db)
) -> AdminPromptOut:
    """编辑一条内容，支持部分字段更新。"""
    try:
        prompt = await admin_service.update_prompt(db, prompt_id, body)
    except SubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if prompt is None:
        raise HTTPException(status_code=404, detail="内容不存在")
    return AdminPromptOut.model_validate(prompt)


@router.post("/prompts/{prompt_id}/publish", response_model=AdminPromptOut)
async def publish_prompt(prompt_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AdminPromptOut:
    """一键发布：审核通过后把草稿态内容上线，比完整的编辑接口更轻量。"""
    try:
        prompt = await admin_service.set_published(db, prompt_id, True)
    except SubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if prompt is None:
        raise HTTPException(status_code=404, detail="内容不存在")
    return AdminPromptOut.model_validate(prompt)


@router.post("/prompts/{prompt_id}/unpublish", response_model=AdminPromptOut)
async def unpublish_prompt(prompt_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> AdminPromptOut:
    """下架：把已发布内容撤回为草稿态，用户侧立即不可见。"""
    try:
        prompt = await admin_service.set_published(db, prompt_id, False)
    except SubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if prompt is None:
        raise HTTPException(status_code=404, detail="内容不存在")
    return AdminPromptOut.model_validate(prompt)


@router.delete("/prompts/{prompt_id}", status_code=204)
async def delete_prompt(prompt_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    """彻底删除一条内容（级联删除关联的效果图和举报记录），不可恢复。"""
    deleted = await admin_service.delete_prompt(db, prompt_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="内容不存在")


# ---------- 举报管理 ----------


@router.get("/reports", response_model=AdminReportListResponse)
async def list_reports(
    status: str | None = Query(None, description="筛选处理状态：pending / resolved，不传则返回全部"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AdminReportListResponse:
    """
    后台举报列表，对应 PRD 3.5"下架申诉处理"（P0，24-48h 内处理）。
    默认按创建时间倒序，可用 status=pending 筛选出"待处理"的举报。
    """
    rows, total = await admin_service.list_reports(db, status, page, page_size)
    items = [
        AdminReportOut(
            id=report.id,
            prompt_id=report.prompt_id,
            prompt_title=prompt_title,
            reporter_email=report.reporter_email,
            reason=report.reason,
            status=report.status,
            handled_note=report.handled_note,
            created_at=report.created_at,
            handled_at=report.handled_at,
        )
        for report, prompt_title in rows
    ]
    return AdminReportListResponse(items=items, total=total, page=page, page_size=page_size)


@router.post("/reports/{report_id}/handle", response_model=AdminReportOut)
async def handle_report(
    report_id: uuid.UUID, body: AdminReportHandle, db: AsyncSession = Depends(get_db)
) -> AdminReportOut:
    """
    处理一条举报：标记为已处理，记录处理备注；unpublish_prompt=True 时联动下架对应内容。
    """
    report = await admin_service.handle_report(db, report_id, body.handled_note, body.unpublish_prompt)
    if report is None:
        raise HTTPException(status_code=404, detail="举报记录不存在")

    prompt = await db.get(Prompt, report.prompt_id)
    return AdminReportOut(
        id=report.id,
        prompt_id=report.prompt_id,
        prompt_title=prompt.title if prompt else None,
        reporter_email=report.reporter_email,
        reason=report.reason,
        status=report.status,
        handled_note=report.handled_note,
        created_at=report.created_at,
        handled_at=report.handled_at,
    )


# ---------- 爬虫采集草稿管理 ----------
# 对应 docs/aigc-prompt-爬虫方案.md 的"脚本采集 → 人工审核 → 确认入库"流程。
# 草稿数据由离线爬虫脚本（scripts/crawler/）写入，这里只负责审核之后的操作。


@router.get("/crawl-drafts", response_model=CrawlDraftListResponse)
async def list_crawl_drafts(
    status: str | None = Query(None, description="筛选状态：pending / approved / rejected，不传则返回全部"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> CrawlDraftListResponse:
    """采集草稿列表，默认按创建时间倒序，常用 status=pending 筛选出"待审核"的草稿。"""
    rows, total = await crawl_service.list_drafts(db, status, page, page_size)
    return CrawlDraftListResponse(
        items=[_draft_to_out(draft) for draft in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/crawl-drafts/{draft_id}", response_model=CrawlDraftOut)
async def get_crawl_draft(draft_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CrawlDraftOut:
    """查看单条草稿详情（含原始正文全文、所有配图），审核前先看完整素材。"""
    draft = await crawl_service.get_draft(db, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return _draft_to_out(draft)


@router.patch("/crawl-drafts/{draft_id}", response_model=CrawlDraftOut)
async def update_crawl_draft(
    draft_id: uuid.UUID, body: CrawlDraftReviewUpdate, db: AsyncSession = Depends(get_db)
) -> CrawlDraftOut:
    """
    人工审核时修正草稿字段（从原始正文里整理出提示词原文、标注分类和模型），
    不会创建正式内容，可以反复调用直到字段整理清楚为止。
    """
    draft = await crawl_service.update_draft_review_fields(db, draft_id, body)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return _draft_to_out(draft)


@router.post("/crawl-drafts/{draft_id}/approve", response_model=AdminPromptOut, status_code=201)
async def approve_crawl_draft(
    draft_id: uuid.UUID, body: CrawlDraftApprove, db: AsyncSession = Depends(get_db)
) -> AdminPromptOut:
    """
    审核通过，把草稿转为正式的 Prompt 内容（默认仍是草稿态，需要再调发布接口上线）。

    草稿状态会同步变为 approved，防止重复转化；提示词原文/分类如果请求体没传，
    会用草稿上已经通过 PATCH 接口整理好的 extracted_* 字段。
    """
    try:
        prompt = await crawl_service.approve_draft(db, draft_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if prompt is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return AdminPromptOut.model_validate(prompt)


@router.post("/crawl-drafts/{draft_id}/reject", response_model=CrawlDraftOut)
async def reject_crawl_draft(
    draft_id: uuid.UUID, body: CrawlDraftRejectBody, db: AsyncSession = Depends(get_db)
) -> CrawlDraftOut:
    """忽略一条草稿（不转为正式内容），用于筛掉抓到的低质量/不相关素材。"""
    draft = await crawl_service.reject_draft(db, draft_id, body.note)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return _draft_to_out(draft)


# ---------- 采集任务（点击按钮触发 MediaCrawler，支持多平台） ----------
# 对应用户需求"点点按钮，等待后，数据就回来"，且可以多选平台（小红书/微博/快手）。
# 这里只负责创建任务和查询任务状态，真正的采集逻辑（拉起 MediaCrawler 子进程 +
# 导入草稿表）在 crawl_task_service。

# 平台展示标签，和 crawl_task_service.PLATFORM_SPECS 的 key 保持一致
PLATFORM_LABELS: dict[str, str] = {
    "xiaohongshu": "小红书",
    "weibo": "微博",
    "kuaishou": "快手",
}


@router.get("/crawl-platforms", response_model=CrawlPlatformListResponse)
async def list_crawl_platforms() -> CrawlPlatformListResponse:
    """
    返回当前支持的采集平台列表，前端用这个渲染多选框（而不是硬编码在前端代码里），
    后端加/减支持的平台时前端不需要跟着改。
    """
    warnings = crawl_task_service.get_platform_warnings()
    return CrawlPlatformListResponse(
        platforms=[
            CrawlPlatformInfo(value=key, label=label, warning=warnings.get(key))
            for key, label in PLATFORM_LABELS.items()
        ]
    )


@router.post("/crawl-tasks", response_model=list[CrawlTaskOut], status_code=201)
async def start_crawl_task(
    body: CrawlTaskStartRequest, db: AsyncSession = Depends(get_db)
) -> list[CrawlTaskOut]:
    """
    开始一批采集任务（按勾选的平台分别创建一条，共享同一个 batch_id），立即
    返回全部任务记录（status=pending/running），不等待采集完成。

    前端应该拿到返回的任务列表，对每个 task.id 分别轮询 GET /api/admin/crawl-tasks/{id}，
    直到所有任务都变为 success 或 failed。
    """
    try:
        tasks = await crawl_task_service.start_crawl_batch(db, body.platforms, body.keyword, body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [CrawlTaskOut.model_validate(task) for task in tasks]


@router.get("/crawl-tasks", response_model=CrawlTaskListResponse)
async def list_crawl_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> CrawlTaskListResponse:
    """采集任务历史列表，按创建时间倒序，方便运营方回顾之前跑过哪些关键词。"""
    rows, total = await crawl_task_service.list_crawl_tasks(db, page, page_size)
    return CrawlTaskListResponse(
        items=[CrawlTaskOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/crawl-tasks/{task_id}", response_model=CrawlTaskOut)
async def get_crawl_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CrawlTaskOut:
    """查询单个采集任务的当前状态，前端轮询用这个接口。"""
    task = await crawl_task_service.get_crawl_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return CrawlTaskOut.model_validate(task)
