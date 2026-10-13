"""
全局配置模块。

所有环境变量在这里统一读取和校验，禁止在业务代码里直接用 os.getenv 读取配置，
保证配置来源单一、可测试、可在 .env.example 中集中查阅。
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，字段名与 .env 中的环境变量名一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 数据库 ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/aigc_prompt"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- JWT ---
    jwt_secret_key: str = "CHANGE_ME_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expire_days: int = 30

    # --- Google OAuth ---
    google_client_id: str = ""
    google_certs_url: str = "https://www.googleapis.com/oauth2/v1/certs"

    @field_validator("google_certs_url")
    @classmethod
    def validate_google_certs_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Google certificate URL must use HTTPS")
        return value

    # --- 邮件服务（Resend） ---
    resend_api_key: str = ""
    email_from: str = "noreply@aigc-prompt.com"

    # --- 腾讯云 COS（对象存储，用于正式发布内容的图片）---
    # 2026-10-09 确定改用腾讯云 COS（原计划阿里云 OSS 一直未申请账号，没有实际
    # 使用过，代码里也没有基于 oss2 写过任何调用，直接切换不存在历史负担）。
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = ""  # 完整 bucket 名，含随机后缀，如 aigc-prompt-images-1310986640
    cos_region: str = "ap-guangzhou"
    # 自定义 CDN 域名（接了 CDN 加速后再填；留空则直接用 COS 默认访问域名）
    cos_cdn_domain: str = ""

    # --- 应用基础 ---
    app_env: str = "development"  # development / production
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- 后台管理鉴权 ---
    # MVP 阶段只有运营方（你自己）一个人用后台，不需要完整的用户角色体系，
    # 用一个固定密钥校验请求头即可，详见 app/utils/admin_auth.py。
    admin_secret_key: str = "CHANGE_ME_IN_PRODUCTION"

    # --- 爬虫采集本地存储 ---
    # 采集阶段（草稿未审核前）的图片始终先存本机这个目录，由 app/main.py 挂载的
    # 静态文件路由对外提供访问；只有审核通过转为正式内容时才会上传到 COS，
    # 详见 app/services/cos_service.py 和 docs/aigc-prompt-爬虫方案.md。
    crawl_media_dir: str = "./crawl_media"

    # --- MediaCrawler 集成（后台一键采集） ---
    # MediaCrawler 是独立的第三方项目（NON-COMMERCIAL LEARNING LICENSE 1.1 许可），
    # 不是本项目的子目录，这里只是记录它的本机路径，供 crawl_task_service 用
    # subprocess 调用它的 main.py，详见 app/services/crawl_task_service.py 顶部说明。
    mediacrawler_dir: str = "~/Desktop/MediaCrawler"
    chrome_binary: str = ""
    chrome_headless: bool = False
    crawl_timeout_seconds: int = 900
    crawl_timezone: str = "Asia/Shanghai"

    # --- 阿里云百炼（DashScope）—— 内容审核用视觉理解模型 ---
    # 2026-10-09 接入，用于草稿审核通过前的 AI 预审：图片真实性判断 + 文案清洗 + 分类建议。
    # 使用 qwen3.7-plus 多模态模型，通过 OpenAI 兼容接口调用，详见
    # app/services/content_review_service.py 和 docs/aigc-prompt-内容审核方案.md。
    dashscope_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例（进程内缓存，避免重复解析 .env）。"""
    return Settings()
