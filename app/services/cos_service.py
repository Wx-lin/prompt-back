"""
腾讯云 COS 上传服务。

用途：草稿审核通过转为正式内容时，把本地文件（MediaCrawler 采集下载到
`MEDIACRAWLER_DIR/data/...` 的图片）上传到 COS，拿到一个公网可访问的 URL，
替换掉之前"直接复用本地路径当 URL"的临时方案（见 crawl_service.py 的历史注释）。

为什么只在"审核通过"这一步上传，不在采集阶段就上传：
- 草稿阶段的素材大部分会被忽略（运营方人工筛选），没必要把所有抓到的图片
  都占用 COS 存储和流量，只有真正要发布成正式内容的图片才值得上传。
- 本地静态文件路由（/mediacrawler-media/...）继续负责草稿审核阶段的图片预览，
  不受这次改动影响。

设计取舍：
- 用腾讯云官方 SDK `cos-python-sdk-v5`（同步 API），上传操作用
  `asyncio.to_thread` 包一层避免阻塞事件循环——这类 SDK 没有原生 async 接口，
  没必要为了这一个上传功能自己拿 httpx 重新实现签名算法。
- 没有配置 COS 密钥时（cos_secret_id/cos_bucket 为空）直接抛出明确的错误，
  不做"静默回退成本地路径"这种容易被忽略的降级，审核通过操作应该让运营方
  清楚知道"图片没有真正上传成功"，而不是看起来成功了但其实还是本地临时路径。
"""

import asyncio
import mimetypes
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from qcloud_cos import CosConfig, CosS3Client

from app.config import get_settings

settings = get_settings()


class CosUploadError(Exception):
    """COS 上传失败或未配置时抛出，message 面向运营方可读。"""


def _build_client() -> CosS3Client:
    if not (settings.cos_secret_id and settings.cos_secret_key and settings.cos_bucket):
        raise CosUploadError(
            "腾讯云 COS 尚未配置（COS_SECRET_ID / COS_SECRET_KEY / COS_BUCKET），"
            "请先在 .env 中填好再重试审核通过操作。"
        )
    config = CosConfig(
        Region=settings.cos_region,
        SecretId=settings.cos_secret_id,
        SecretKey=settings.cos_secret_key,
    )
    return CosS3Client(config)


def _public_url(object_key: str) -> str:
    """拼接上传后的公网访问 URL：优先用自定义 CDN 域名，没配置就用 COS 默认域名。"""
    if settings.cos_cdn_domain:
        domain = settings.cos_cdn_domain.rstrip("/")
        return f"{domain}/{object_key}"
    return f"https://{settings.cos_bucket}.cos.{settings.cos_region}.myqcloud.com/{object_key}"


def _upload_sync(local_path: Path, object_key: str) -> str:
    client = _build_client()
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    with local_path.open("rb") as f:
        client.put_object(
            Bucket=settings.cos_bucket,
            Body=f,
            Key=object_key,
            ContentType=content_type,
        )
    return _public_url(object_key)


def _upload_bytes_sync(body: bytes, object_key: str, content_type: str) -> str:
    client = _build_client()
    client.put_object(
        Bucket=settings.cos_bucket,
        Body=body,
        Key=object_key,
        ContentType=content_type,
    )
    return _public_url(object_key)


async def upload_prompt_image(local_path: Path) -> str:
    """
    上传一张正式内容的配图到 COS，返回公网访问 URL。

    object_key 规则：prompts/{随机uuid}{原扩展名}，不用本地文件名（可能重复/带
    特殊字符），也不保留原始目录结构（草稿阶段的本地路径是内部实现细节，
    不需要暴露到 COS 的对象键里）。
    """
    if not local_path.exists():
        raise CosUploadError(f"本地图片文件不存在：{local_path}")

    object_key = f"prompts/{uuid.uuid4().hex}{local_path.suffix.lower()}"
    return await asyncio.to_thread(_upload_sync, local_path, object_key)


async def migrate_remote_image_to_cos(remote_url: str) -> str:
    """
    把一张还停留在第三方 CDN（如小红书 xhscdn.com）的图片下载下来，转存到 COS。

    用途：2026-10-09 批量清理存量下线数据时发现，部分旧数据的 image_url 当时
    直接复用了采集来源的 CDN 直链，没有走过本服务的 upload_prompt_image——
    这类直链通常带防盗链机制，第三方服务（如内容审核用的多模态模型）无法
    直接下载，必须先转存到自己可控的 COS 才能被外部服务稳定访问。

    带一个常见浏览器 User-Agent 请求头，规避源站的防盗链校验（只认 Referer/UA，
    不是签名级别的防护）；如果源站本身做了更严格的防护导致仍然下载失败，
    这里会原样把 httpx 的异常包装成 CosUploadError 往上抛，不做静默重试。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.xiaohongshu.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(remote_url, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise CosUploadError(f"下载远程图片失败（{remote_url}）：{exc}") from exc

    content_type = resp.headers.get("content-type", "").split(";")[0] or "image/jpeg"
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    object_key = f"prompts/{uuid.uuid4().hex}{ext}"
    return await asyncio.to_thread(_upload_bytes_sync, resp.content, object_key, content_type)


async def upload_author_avatar(remote_url: str) -> str:
    """Copy a crawler-supplied platform avatar to COS before publishing it."""
    host = (urlparse(remote_url).hostname or "").lower()
    allowed_suffixes = (
        "xhscdn.com",
        "sinaimg.cn",
        "weibo.cn",
        "kscdn.com",
        "gifshow.com",
        "kwaicdn.com",
        "yximgs.com",
        "kwimgs.com",
    )
    if not remote_url.startswith("https://") or not any(
        host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes
    ):
        raise CosUploadError("头像地址不是支持的平台图片域名")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            async with client.stream("GET", remote_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                    raise CosUploadError("头像响应不是支持的图片格式")
                chunks = []
                total_size = 0
                async for chunk in response.aiter_bytes():
                    total_size += len(chunk)
                    if total_size > 2_000_000:
                        raise CosUploadError("头像文件过大")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise CosUploadError(f"下载作者头像失败：{type(exc).__name__}") from exc
    extension = mimetypes.guess_extension(content_type) or ".jpg"
    object_key = f"author-avatars/{uuid.uuid4().hex}{extension}"
    return await asyncio.to_thread(_upload_bytes_sync, b"".join(chunks), object_key, content_type)


async def upload_submission_image(body: bytes, object_key: str) -> str:
    """Unreviewed uploads are private even when the bucket default is public."""

    def upload():
        client = _build_client()
        client.put_object(
            Bucket=settings.cos_bucket, Body=body, Key=object_key, ContentType="image/jpeg", ACL="private"
        )
        return _public_url(object_key)

    return await asyncio.to_thread(upload)


def submission_preview_url(object_key: str) -> str:
    return _build_client().get_presigned_url(
        Method="GET", Bucket=settings.cos_bucket, Key=object_key, Expired=600
    )


async def set_submission_visibility(object_key: str, public: bool):
    await asyncio.to_thread(
        _build_client().put_object_acl,
        Bucket=settings.cos_bucket,
        Key=object_key,
        ACL="public-read" if public else "private",
    )


async def delete_submission_image(object_key: str):
    await asyncio.to_thread(_build_client().delete_object, Bucket=settings.cos_bucket, Key=object_key)
