"""Only server-selected public gallery COS objects can become private references."""

import io
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from starlette.datastructures import Headers, UploadFile

from app.config import get_settings
from app.models import Prompt, PromptImage
from app.services.prompt_analysis import assets, limits, storage
from app.services.prompt_analysis.errors import AnalysisError
from app.utils.public_prompts import public_prompt_condition


def unavailable():
    return AnalysisError(422, "SOURCE_IMAGE_UNAVAILABLE", "该作品暂不支持带图创作，请上传参考图片")


def object_key(url):
    settings = get_settings()
    roots = [f"https://{settings.cos_bucket}.cos.{settings.cos_region}.myqcloud.com/"]
    if settings.cos_cdn_domain:
        roots.append(settings.cos_cdn_domain.rstrip("/") + "/")
    try:
        source = urlsplit(url)
        if (
            source.scheme != "https"
            or source.username
            or source.password
            or source.query
            or source.fragment
            or source.port not in {None, 443}
        ):
            raise unavailable()
        for root_url in roots:
            root = urlsplit(root_url)
            if (
                root.scheme == "https"
                and source.netloc.lower() == root.netloc.lower()
                and source.path.startswith(root.path)
            ):
                key = source.path[len(root.path) :]
                if re.fullmatch(
                    r"(?:prompts|submissions|library)/[A-Za-z0-9][A-Za-z0-9._/-]{0,230}\.(?:jpe?g|png|webp)",
                    key,
                    flags=re.IGNORECASE,
                ) and not any(part in {"", ".", ".."} for part in key.split("/")):
                    return key
    except ValueError:
        pass
    raise unavailable()


async def import_prompt_asset(db, redis, user_id, prompt_id):
    assets.ensure_enabled()
    storage.require_storage_configuration()
    await limits.upload_limit(redis, user_id)
    url = await db.scalar(
        select(PromptImage.image_url)
        .join(Prompt)
        .where(
            Prompt.id == prompt_id,
            public_prompt_condition(),
        )
        .order_by(PromptImage.sort_order, PromptImage.id)
        .limit(1)
    )
    if not url:
        raise assets.not_found()
    key = object_key(url)
    try:
        body, content_type = await storage.read_public_prompt(key)
    except AnalysisError:
        raise
    except Exception:
        raise AnalysisError(503, "STORAGE_UNAVAILABLE", "参考图读取失败，请稍后重试") from None
    file = UploadFile(io.BytesIO(body), filename="reference", headers=Headers({"content-type": content_type}))
    return await assets.store_upload(db, user_id, file)
