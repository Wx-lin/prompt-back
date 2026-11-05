"""SCF image generation keeps payloads private and never retries paid work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.services import scf_image_adapter

PNG = b'\x89PNG\r\n\x1a\nvalid-image'


class FakeObjectBody:
    def get_raw_stream(self):
        from io import BytesIO
        return BytesIO(PNG)


class FakeCos:
    def __init__(self):
        self.signed = []
        self.put = []
        self.deleted = []

    def get_presigned_url(self, **kwargs):
        self.signed.append(kwargs)
        return 'https://private-bucket.cos.example/' + kwargs['Key'] + '?signature=secret'

    def put_object(self, **kwargs):
        self.put.append(kwargs)

    def get_object(self, **kwargs):
        return {'Body': FakeObjectBody()}

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)


@pytest.fixture
def scf_settings():
    return SimpleNamespace(
        image_scf_enabled=True, image_scf_cos_bucket=scf_image_adapter.VERIFIED_BUCKET,
        image_scf_cos_region='ap-singapore',
        image_scf_region='ap-singapore', image_scf_function_name='image-generator',
        image_scf_namespace='default',
        image_scf_secret_id=SecretStr('id'), image_scf_secret_key=SecretStr('key'),
        image_scf_cos_secret_id=SecretStr('cos-id'), image_scf_cos_secret_key=SecretStr('cos-key'),
        cos_bucket='public-bucket', openai_api_key=SecretStr(''),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('reference', [None, b'\x89PNG\r\n\x1a\nreference'])
async def test_scf_request_uses_bounded_private_cos_objects(monkeypatch, scf_settings, reference):
    import asyncio

    client = FakeCos()
    invoke = AsyncMock(return_value={'status': 'ok'})
    monkeypatch.setattr(scf_image_adapter, '_build_cos_client', lambda settings: client)
    monkeypatch.setattr(scf_image_adapter, '_invoke', invoke)
    result = await scf_image_adapter.generate('person', '1024x1024', reference, 120, scf_settings)
    await asyncio.gather(*scf_image_adapter._CLEANUP_TASKS)
    assert result == PNG
    assert invoke.await_count == 1
    event = invoke.await_args.args[0]
    job_id = event['job_id']
    assert len(job_id) == 32 and all(c in '0123456789abcdef' for c in job_id)
    assert event['operation'] == ('edits' if reference else 'generations')
    assert event['model'] == 'gpt-image-2' and event['n'] == 1 and event['quality'] == 'low'
    assert event['claim_put_url'].split('?')[0].endswith(f'image-relay/{job_id}/claim')
    assert event['output_put_url'].split('?')[0].endswith(f'image-relay/{job_id}/output')
    for item in client.signed:
        assert item['Bucket'] == scf_image_adapter.VERIFIED_BUCKET and item['SignHost'] is True
        if item['Method'] == 'PUT':
            assert item['Headers'] == {'x-cos-acl': 'private', 'x-cos-forbid-overwrite': 'true'}
    assert len(client.put) == (1 if reference else 0)
    if reference:
        assert client.put[0]['ACL'] == 'private' and client.put[0]['Body'] == reference
        assert event['reference_get_url'].split('?')[0].endswith(f'image-relay/{job_id}/reference')
    assert all(not obj['Key'].endswith('/claim') for obj in client.deleted)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [{'status': 'duplicate'}, {'status': 'error', 'code': 'provider_timeout'}])
async def test_scf_errors_are_uncertain_and_never_retry_or_delete_claim(monkeypatch, scf_settings, status):
    client = FakeCos()
    invoke = AsyncMock(return_value=status)
    monkeypatch.setattr(scf_image_adapter, '_build_cos_client', lambda settings: client)
    monkeypatch.setattr(scf_image_adapter, '_invoke', invoke)
    with pytest.raises(scf_image_adapter.ScfImageError) as error:
        await scf_image_adapter.generate('person', '1024x1024', None, 120, scf_settings)
    assert error.value.uncertain is True
    assert invoke.await_count == 1
    assert not client.deleted


@pytest.mark.asyncio
async def test_cleanup_timeout_never_discards_successful_image(monkeypatch, scf_settings):
    import asyncio
    import threading

    client = FakeCos()
    release = threading.Event()
    def slow_delete(**kwargs):
        release.wait(2)
    client.delete_object = slow_delete
    monkeypatch.setattr(scf_image_adapter, '_build_cos_client', lambda settings: client)
    monkeypatch.setattr(scf_image_adapter, '_invoke', AsyncMock(return_value={'status': 'ok'}))
    monkeypatch.setattr(scf_image_adapter, 'CLEANUP_TIMEOUT', 1.0, raising=False)
    try:
        result = await asyncio.wait_for(
            scf_image_adapter.generate('person', '1024x1024', None, 0.1, scf_settings), timeout=0.3,
        )
        assert result == PNG
    finally:
        release.set()
        await asyncio.gather(*scf_image_adapter._CLEANUP_TASKS, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_scheduling_failure_never_discards_successful_image(monkeypatch, scf_settings):
    monkeypatch.setattr(scf_image_adapter, '_build_cos_client', lambda settings: FakeCos())
    monkeypatch.setattr(scf_image_adapter, '_invoke', AsyncMock(return_value={'status': 'ok'}))
    def fail(*args):
        raise RuntimeError('cleanup unavailable')
    monkeypatch.setattr(scf_image_adapter, '_schedule_cleanup', fail)
    assert await scf_image_adapter.generate('person', '1024x1024', None, 5, scf_settings) == PNG


def test_scf_disabled_by_default():
    from app.config import Settings
    assert Settings(_env_file=None).image_scf_enabled is False


def test_scf_without_private_configuration_fails_closed(scf_settings):
    assert scf_image_adapter.image_provider_available(scf_settings)
    scf_settings.image_scf_cos_bucket = scf_settings.cos_bucket
    scf_settings.openai_api_key = SecretStr('test-key')
    assert not scf_image_adapter.image_provider_available(scf_settings)


def test_cos_client_has_finite_timeout_and_no_retries(scf_settings):
    client = scf_image_adapter._build_cos_client(scf_settings)
    assert client._conf._timeout == 15
    assert client._retry == 0
    assert client._session.trust_env is False


def test_cos_client_does_not_modify_other_clients_shared_session(scf_settings):
    from qcloud_cos import CosConfig, CosS3Client

    other = CosS3Client(CosConfig(Region='ap-guangzhou', SecretId='test-id', SecretKey='test-key'))
    other_initial = other._session.trust_env
    client = scf_image_adapter._build_cos_client(scf_settings)
    assert client._session is not other._session
    assert other._session.trust_env == other_initial


@pytest.mark.asyncio
async def test_canceled_sdk_call_holds_bounded_capacity_until_thread_returns(monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=1)
    capacity = threading.BoundedSemaphore(1)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(scf_image_adapter, '_SDK_EXECUTOR', executor)
    monkeypatch.setattr(scf_image_adapter, '_SDK_CAPACITY', capacity)
    def slow():
        started.set()
        release.wait(5)
    first = asyncio.create_task(scf_image_adapter._sdk_call(slow))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(scf_image_adapter.ScfImageError):
            await scf_image_adapter._sdk_call(lambda: None)
    finally:
        release.set()
        executor.shutdown(wait=True)


@pytest.mark.parametrize(('bucket', 'region', 'function_region'), [
    ('any-other-bucket', 'ap-singapore', 'ap-singapore'),
    (scf_image_adapter.VERIFIED_BUCKET, 'ap-guangzhou', 'ap-singapore'),
    (scf_image_adapter.VERIFIED_BUCKET, 'ap-singapore', 'ap-guangzhou'),
])
def test_unverified_bucket_or_region_fails_closed(scf_settings, bucket, region, function_region):
    scf_settings.image_scf_cos_bucket = bucket
    scf_settings.image_scf_cos_region = region
    scf_settings.image_scf_region = function_region
    assert not scf_image_adapter.image_provider_available(scf_settings)


@pytest.mark.parametrize('invoke_result', [None, 0, 200])
def test_scf_sdk_invocation_is_sync_and_has_no_automatic_retry(monkeypatch, scf_settings, invoke_result):
    import json

    from tencentcloud.common.retry import NoopRetryer
    from tencentcloud.scf.v20180416.scf_client import ScfClient

    calls = []
    def invoke(client, request):
        calls.append((client, request))
        return SimpleNamespace(Result=SimpleNamespace(
            ErrMsg='', InvokeResult=invoke_result, RetMsg='{"status":"ok"}',
        ))
    monkeypatch.setattr(ScfClient, 'InvokeFunction', invoke)
    result = scf_image_adapter._invoke_sync({'job_id': 'abc'}, scf_settings, 120)
    assert result == {'status': 'ok'} and len(calls) == 1
    assert isinstance(calls[0][0].profile.retryer, NoopRetryer)
    assert calls[0][0].profile.httpProfile.reqTimeout == 120
    assert calls[0][0].request.conn.proxy is None
    assert calls[0][0].request.conn._session.trust_env is False
    assert calls[0][0].request.conn._session.max_redirects == 0
    assert calls[0][1].FunctionName == 'image-generator' and calls[0][1].LogType == 'None'
    assert json.loads(calls[0][1].Event) == {'job_id': 'abc'}


def test_scf_sdk_rejects_function_error_even_with_success_code(monkeypatch, scf_settings):
    from tencentcloud.scf.v20180416.scf_client import ScfClient

    monkeypatch.setattr(ScfClient, 'InvokeFunction', lambda client, request: SimpleNamespace(
        Result=SimpleNamespace(ErrMsg='private failure', InvokeResult=200, RetMsg='{"status":"ok"}'),
    ))
    with pytest.raises(scf_image_adapter.ScfImageError) as error:
        scf_image_adapter._invoke_sync({'job_id': 'abc'}, scf_settings, 120)
    assert error.value.uncertain is True and 'private failure' not in str(error.value)


def test_tencent_sdk_logs_are_filtered_during_private_operations(caplog):
    import logging

    from app.services.prompt_analysis.storage import _private_sdk_operation

    logger = logging.getLogger('tencentcloud_sdk_common')
    with caplog.at_level(logging.DEBUG, logger='tencentcloud_sdk_common'):
        with _private_sdk_operation():
            logger.debug('temporary-url-must-not-appear')
        logger.debug('normal-log-visible')
    assert 'temporary-url-must-not-appear' not in caplog.text
    assert 'normal-log-visible' in caplog.text


@pytest.mark.asyncio
async def test_private_log_filter_context_propagates_to_sdk_thread(caplog):
    import logging

    from app.services.prompt_analysis.storage import _private_sdk_operation

    logger = logging.getLogger('tencentcloud_sdk_common')
    with caplog.at_level(logging.DEBUG, logger='tencentcloud_sdk_common'):
        with _private_sdk_operation():
            await scf_image_adapter._sdk_call(logger.debug, 'thread-url-must-not-appear')
    assert 'thread-url-must-not-appear' not in caplog.text


@pytest.mark.asyncio
async def test_character_preview_switches_to_scf_without_openai_key(monkeypatch, scf_settings):
    import base64

    from app.services import character_preview

    generate = AsyncMock(return_value=PNG)
    monkeypatch.setattr(character_preview, 'get_settings', lambda: scf_settings)
    monkeypatch.setattr(scf_image_adapter, 'generate', generate)
    assert await character_preview.generate_image('person', '', '1024x1024') == base64.b64encode(PNG).decode()
    assert generate.await_args.args[:4] == ('person', '1024x1024', None, 150)


@pytest.mark.asyncio
async def test_chat_image_switches_to_scf_without_openai_key(monkeypatch, scf_settings):
    from app.services.vision import image_adapter

    generate = AsyncMock(return_value=PNG)
    monkeypatch.setattr(image_adapter, 'get_settings', lambda: scf_settings)
    monkeypatch.setattr(scf_image_adapter, 'generate', generate)
    assert await image_adapter.generate('person', api_key='', reference=PNG, timeout=120) == PNG
    assert generate.await_args.args[:4] == ('person', '1024x1024', PNG, 120)
