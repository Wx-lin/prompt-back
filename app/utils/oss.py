"""
阿里云 OSS 工具：生成图片访问 URL、缩略图 URL。

MVP 阶段内容由运营方手动上传到 OSS 后，直接把 URL 写入数据库，这里只提供
"根据原图 URL 拼出缩略图 URL"的辅助函数，暂不实现自动上传逻辑（见
后端技术选型.md 第五节：MVP 阶段为手动录入，不走代码上传）。
"""

from app.config import get_settings


def build_thumbnail_url(original_url: str, width: int = 400) -> str:
    """
    利用 OSS 图片处理参数，在原图 URL 后拼接 resize 参数生成缩略图地址，
    不需要额外存储一份缩略图文件。

    示例：
        build_thumbnail_url("https://cdn.xxx.com/a.jpg")
        -> "https://cdn.xxx.com/a.jpg?x-oss-process=image/resize,w_400"
    """
    return f"{original_url}?x-oss-process=image/resize,w_{width}"


def to_cdn_url(oss_object_key: str) -> str:
    """将 OSS 对象 key 转换为对外可访问的 CDN 完整 URL。"""
    settings = get_settings()
    return f"{settings.oss_cdn_domain.rstrip('/')}/{oss_object_key.lstrip('/')}"
