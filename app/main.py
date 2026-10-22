"""
FastAPI 应用入口：组装路由、中间件、生命周期事件（启动/关闭定时任务）。

启动方式：
    uv run uvicorn app.main:app --reload --port 8000

启动后可访问：
    http://localhost:8000/docs   自动生成的 Swagger API 文档
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routers import (
    admin,
    admin_insights,
    admin_submissions,
    auth,
    categories,
    crawl_operations,
    model_catalog,
    prompt_analyses,
    prompts,
    search,
    seo,
    submissions,
    traffic,
    users,
)
from app.scheduler import create_scheduler
from app.utils.prompt_analysis_http import PromptAnalysisBoundary
from app.utils.submission_body_limit import SubmissionBodyLimit

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时开启定时任务调度器，关闭时停止，避免资源泄漏。"""
    scheduler = create_scheduler()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="AIGC提示词 API",
    description="aigc-prompt.com 后端服务，详见项目根目录 README.md 和 docs/ 下的设计文档。",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SubmissionBodyLimit)
app.add_middleware(PromptAnalysisBoundary)


@app.middleware("http")
async def prevent_seo_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/admin/seo", "/api/admin/traffic", "/api/analytics/visit")):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(submissions.router)
app.include_router(prompt_analyses.router)
app.include_router(admin_submissions.router)
app.include_router(prompts.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(categories.router)
app.include_router(model_catalog.router)
app.include_router(search.router)
app.include_router(admin.router)
app.include_router(admin_insights.router)
app.include_router(seo.router)
app.include_router(traffic.router)
app.include_router(crawl_operations.router)

# 爬虫采集图片的本地静态文件服务：OSS 密钥配置好之前的临时方案，仅用于本地开发，
# 生产环境不应该用这种方式对外提供图片访问，详见 docs/aigc-prompt-爬虫方案.md。
_crawl_media_path = Path(settings.crawl_media_dir)
_crawl_media_path.mkdir(parents=True, exist_ok=True)
app.mount("/local-media", StaticFiles(directory=str(_crawl_media_path)), name="local-media")

# MediaCrawler（第三方爬虫工具，独立项目）下载的图片目录，供草稿审核页面展示
# 微博/快手等平台采集到的本地图片。只在目录已存在时才挂载，避免 MediaCrawler
# 还没跑过一次采集时（data 目录不存在）导致应用启动失败。
_mediacrawler_data_path = Path(settings.mediacrawler_dir).expanduser().resolve() / "data"
if _mediacrawler_data_path.exists():
    app.mount(
        "/mediacrawler-media", StaticFiles(directory=str(_mediacrawler_data_path)), name="mediacrawler-media"
    )


@app.get("/health")
async def health_check() -> dict:
    """健康检查接口，供部署平台/负载均衡器探活使用。"""
    return {"status": "ok"}
