"""One synchronous SCF image request with private COS transfer and no retries."""

import asyncio
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial

import requests
from qcloud_cos import CosConfig, CosS3Client

from app.services.prompt_analysis.storage import _private_sdk_operation

logger = logging.getLogger(__name__)
MAX_IMAGE = 5 * 1024 * 1024
MAX_PREVIEW_IMAGE = 15 * 1024 * 1024
SIGN_TTL = 300
CLEANUP_TIMEOUT = 2
PUT_HEADERS = {"x-cos-acl": "private", "x-cos-forbid-overwrite": "true"}
VERIFIED_BUCKET = "aigc-image-relay-temp-1310986640"
VERIFIED_REGION = "ap-singapore"
_SDK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="image-scf")
_SDK_CAPACITY = threading.BoundedSemaphore(4)
_CLEANUP_TASKS: set[asyncio.Task] = set()


class ScfImageError(Exception):
    def __init__(self, code: str, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


def configured(settings) -> bool:
    if not getattr(settings, "image_scf_enabled", False):
        return False
    values = (
        settings.image_scf_function_name, settings.image_scf_cos_bucket,
        settings.image_scf_secret_id.get_secret_value(), settings.image_scf_secret_key.get_secret_value(),
        settings.image_scf_cos_secret_id.get_secret_value(),
        settings.image_scf_cos_secret_key.get_secret_value(),
    )
    return bool(
        all(value.strip() for value in values)
        and settings.image_scf_cos_bucket == VERIFIED_BUCKET
        and settings.image_scf_cos_bucket != settings.cos_bucket
        and settings.image_scf_cos_region == VERIFIED_REGION
        and settings.image_scf_region == VERIFIED_REGION
    )


def image_provider_available(settings) -> bool:
    if getattr(settings, "image_scf_enabled", False):
        return configured(settings)
    key = getattr(settings, "openai_api_key", None)
    return bool(key and key.get_secret_value().strip())


def _build_cos_client(settings):
    session = requests.Session()
    session.trust_env = False
    session.max_redirects = 0
    client = CosS3Client(CosConfig(
        Region=settings.image_scf_cos_region,
        SecretId=settings.image_scf_cos_secret_id.get_secret_value(),
        SecretKey=settings.image_scf_cos_secret_key.get_secret_value(),
        Timeout=15,
    ), retry=0, session=session)
    return client


async def _sdk_call(function, *args, **kwargs):
    capacity = _SDK_CAPACITY
    if not capacity.acquire(blocking=False):
        raise ScfImageError("image_provider_busy", uncertain=True)
    try:
        loop = asyncio.get_running_loop()
        context = copy_context()
        future = loop.run_in_executor(_SDK_EXECUTOR, context.run, partial(function, *args, **kwargs))
    except BaseException:
        capacity.release()
        raise
    def completed(task):
        capacity.release()
        if not task.cancelled():
            task.exception()
    future.add_done_callback(completed)
    # shield keeps the capacity reserved while a cancelled SDK thread is still running.
    return await asyncio.shield(future)


def _invoke_sync(event, settings, timeout):
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.retry import NoopRetryer
    from tencentcloud.scf.v20180416 import models, scf_client

    auth = credential.Credential(
        settings.image_scf_secret_id.get_secret_value(), settings.image_scf_secret_key.get_secret_value(),
    )
    profile = ClientProfile()
    profile.retryer = NoopRetryer()
    profile.httpProfile.reqTimeout = max(1, int(timeout))
    client = scf_client.ScfClient(auth, settings.image_scf_region, profile)
    # Do not follow redirects or environment proxies with signed URLs in the event.
    client.request.conn.proxy = None
    client.request.conn._session.trust_env = False
    client.request.conn._session.max_redirects = 0
    request = models.InvokeFunctionRequest()
    request.FunctionName = settings.image_scf_function_name
    request.Namespace = settings.image_scf_namespace
    request.LogType = "None"
    request.Event = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    response = client.InvokeFunction(request)
    # InvokeResult is deprecated by Tencent and observed as both 0 and 200 on success.
    if not response.Result or (response.Result.ErrMsg and response.Result.ErrMsg.strip()):
        raise ScfImageError("image_provider_error", uncertain=True)
    return json.loads(response.Result.RetMsg)


async def _invoke(event, settings, timeout):
    return await _sdk_call(_invoke_sync, event, settings, timeout)


def _read_image(client, bucket, key, maximum):
    response = client.get_object(Bucket=bucket, Key=key)
    stream = response["Body"].get_raw_stream()
    try:
        body = stream.read(maximum + 1)
    finally:
        stream.close()
    if len(body) > maximum or not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ScfImageError("image_invalid_output", uncertain=True)
    return body


async def _cleanup(client, bucket, keys):
    try:
        async with asyncio.timeout(CLEANUP_TIMEOUT):
            for key in keys:
                try:
                    await _sdk_call(client.delete_object, Bucket=bucket, Key=key)
                except Exception:
                    logger.warning("Could not remove temporary image object")
    except TimeoutError:
        logger.warning("Temporary image cleanup timed out")


def _schedule_cleanup(client, bucket, keys):
    task = asyncio.create_task(_cleanup(client, bucket, keys))
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)


async def generate(prompt, size, reference, timeout, settings, *, max_output=MAX_IMAGE):
    if not configured(settings):
        raise ScfImageError("image_model_not_configured")
    if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > 48 * 1024:
        raise ScfImageError("image_invalid_input")
    if reference and len(reference) > MAX_IMAGE:
        raise ScfImageError("image_invalid_input")
    bucket = settings.image_scf_cos_bucket
    job_id = uuid.uuid4().hex
    prefix = f"image-relay/{job_id}"
    claim_key, output_key, reference_key = (f"{prefix}/{name}" for name in ("claim", "output", "reference"))
    try:
        with _private_sdk_operation():
            deadline = asyncio.get_running_loop().time() + timeout
            async with asyncio.timeout(timeout):
                client = _build_cos_client(settings)
                if reference:
                    await _sdk_call(
                        client.put_object, Bucket=bucket, Key=reference_key, Body=reference, ACL="private",
                    )
                def sign(key, method):
                    options = {
                        "Bucket": bucket, "Key": key, "Method": method, "Expired": SIGN_TTL, "SignHost": True,
                    }
                    if method == "PUT":
                        options["Headers"] = PUT_HEADERS
                    return client.get_presigned_url(**options)
                event = {
                    "job_id": job_id, "operation": "edits" if reference else "generations",
                    "model": "gpt-image-2",
                    "prompt": prompt, "size": size, "quality": "low", "n": 1,
                    "claim_put_url": await _sdk_call(sign, claim_key, "PUT"),
                    "output_put_url": await _sdk_call(sign, output_key, "PUT"),
                }
                if reference:
                    event["reference_get_url"] = await _sdk_call(sign, reference_key, "GET")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                result = await _invoke(event, settings, remaining)
                if not isinstance(result, dict) or result.get("status") != "ok":
                    if isinstance(result, dict) and result.get("code") == "provider_timeout":
                        raise ScfImageError("provider_timeout", uncertain=True)
                    raise ScfImageError("image_provider_error", uncertain=True)
                body = await _sdk_call(_read_image, client, bucket, output_key, max_output)
            try:
                _schedule_cleanup(client, bucket, [output_key, reference_key] if reference else [output_key])
            except Exception:
                logger.warning("Could not schedule temporary image cleanup")
            return body
    except ScfImageError:
        raise
    except TimeoutError:
        raise ScfImageError("provider_timeout", uncertain=True) from None
    except Exception:
        raise ScfImageError("image_provider_error", uncertain=True) from None
