"""Input-image history is returned as private bytes only to the owning conversation."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import PromptAnalysis, PromptAnalysisAsset
from tests.test_prompt_api_integration import api as api
from tests.test_prompt_api_integration import configured as configured
from tests.test_prompt_api_integration import finish, response_data, upload
from tests.test_prompt_api_integration import limiter as limiter
from tests.test_prompt_api_integration import objects as objects
from tests.test_prompt_api_integration import sessions as sessions
from tests.test_prompt_conversations import create_conversation

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not os.getenv('AIGC_TEST_DATABASE_URL') or not os.getenv('AIGC_TEST_REDIS_URL'),
    reason='Explicit isolated PostgreSQL and Redis URLs required',
)]


async def test_input_download_checks_owner_conversation_retention_and_no_cache(api, sessions, objects):
    asset = await upload(api)
    first = response_data(await create_conversation(
        api, {'message': '参考图', 'asset_id': asset['id'], 'mode': 'smart'}
    ), 202)
    path = f"/api/prompt-conversations/{first['id']}/inputs/{first['id']}/download"
    assert (await api.client.get(path)).status_code == 401
    assert (await api.client.get(path, headers=api.other)).status_code == 404
    result = await api.client.get(path, headers=api.owner)
    assert result.status_code == 200
    assert result.headers['content-type'] == 'image/jpeg'
    assert 'private' in result.headers['cache-control'] and 'no-store' in result.headers['cache-control']
    assert result.content.startswith(b'\xff\xd8\xff')
    assert b'signature=' not in result.content

    async with sessions() as db:
        record = await db.get(PromptAnalysisAsset, uuid.UUID(asset['id']))
        original_key = record.object_key
        png_key = original_key.rsplit('/', 1)[0] + '/reference.png'
        record.object_key = png_key
        await db.commit()
    objects[png_key] = b'\x89PNG\r\n\x1a\nreference'
    png = await api.client.get(path, headers=api.owner)
    assert png.status_code == 200 and png.headers['content-type'] == 'image/png'
    assert png.content == objects[png_key]
    async with sessions() as db:
        record = await db.get(PromptAnalysisAsset, uuid.UUID(asset['id']))
        record.object_key = original_key
        record.state = 'deleting'
        await db.commit()
    assert (await api.client.get(path, headers=api.owner)).status_code == 404
    async with sessions() as db:
        record = await db.get(PromptAnalysisAsset, uuid.UUID(asset['id']))
        record.state = 'ready'
        await db.commit()

    await finish(sessions, first['id'])
    second = response_data(await create_conversation(api, {'message':'另一个对话','mode':'smart'}), 202)
    wrong_conversation = f"/api/prompt-conversations/{second['id']}/inputs/{first['id']}/download"
    no_image = f"/api/prompt-conversations/{second['id']}/inputs/{second['id']}/download"
    assert (await api.client.get(wrong_conversation, headers=api.owner)).status_code == 404
    assert (await api.client.get(no_image, headers=api.owner)).status_code == 404
    async with sessions() as db:
        job = await db.get(PromptAnalysis, uuid.UUID(first['id']))
        job.image_retain_until = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert (await api.client.get(path, headers=api.owner)).status_code == 404
