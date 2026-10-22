"""Private COS operations. Object keys never pass through the public CDN."""

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar

from app.config import get_settings
from app.services import cos_service
from app.services.prompt_analysis.errors import AnalysisError

_private_operation = ContextVar("private_cos_operation", default=False)


class _PrivateSdkLogFilter(logging.Filter):
    def filter(self, record):
        return not _private_operation.get()


_log_filter = _PrivateSdkLogFilter()


@contextmanager
def _private_sdk_operation():
    # The SDK logs derived signing keys and signed URLs even for local signing.
    # ContextVars propagate through to_thread without suppressing unrelated tasks.
    names = ["qcloud_cos.cos_auth", "qcloud_cos.cos_client", "urllib3.connectionpool"]
    names.extend(
        name
        for name in list(logging.Logger.manager.loggerDict)
        if name.startswith(("qcloud_cos.", "urllib3.", "requests."))
    )
    for name in names:
        logging.getLogger(name).addFilter(_log_filter)
    token = _private_operation.set(True)
    try:
        yield
    finally:
        _private_operation.reset(token)


def require_storage_configuration() -> None:
    settings = get_settings()
    if not all((settings.cos_secret_id, settings.cos_secret_key, settings.cos_bucket)):
        raise AnalysisError(503, "STORAGE_UNAVAILABLE", "图片存储暂不可用")


async def put_private(body: bytes, object_key: str) -> None:
    require_storage_configuration()
    with _private_sdk_operation():
        await cos_service.upload_submission_image(body, object_key)


async def read_private(object_key: str) -> bytes:
    require_storage_configuration()

    def read():
        response = cos_service._build_client().get_object(Bucket=get_settings().cos_bucket, Key=object_key)
        stream = response["Body"].get_raw_stream()
        try:
            body = stream.read(2 * 1024 * 1024 + 1)
            if not body or len(body) > 2 * 1024 * 1024:
                raise ValueError("Invalid stored image size")
            return body
        finally:
            stream.close()

    with _private_sdk_operation():
        return await asyncio.to_thread(read)


async def delete_private(object_key: str) -> None:
    require_storage_configuration()
    with _private_sdk_operation():
        await cos_service.delete_submission_image(object_key)


def preview_url(object_key: str) -> str:
    require_storage_configuration()
    with _private_sdk_operation():
        return cos_service.submission_preview_url(object_key)
