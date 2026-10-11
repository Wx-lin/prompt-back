import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.services import crawl_task_service as service


def test_chrome_config_defaults():
    config = Settings(_env_file=None)
    assert config.chrome_binary == ""
    assert config.chrome_headless is False
    assert config.crawl_timeout_seconds == 900
    assert config.crawl_timezone == "Asia/Shanghai"


def test_chrome_explicit_binary_wins(monkeypatch):
    monkeypatch.setattr(service.settings, "chrome_binary", "/custom/chrome")
    with patch.object(service.Path, "is_file", return_value=True):
        assert service._chrome_binary() == "/custom/chrome"


def test_jsonl_uses_configured_timezone(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 9, 18, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(service, "datetime", Clock)
    assert (
        service._jsonl_path_for_today(service.PLATFORM_SPECS["xiaohongshu"]).name
        == "search_contents_2026-10-10.jsonl"
    )


@pytest.mark.asyncio
async def test_crawler_timeout_cleans_process_group(monkeypatch):
    async def hanging_output():
        await asyncio.sleep(60)
        yield b""

    process = AsyncMock()
    process.pid = 12345
    process.returncode = None
    process.stdout = hanging_output()
    monkeypatch.setattr(service.settings, "crawl_timeout_seconds", 0.01)
    with (
        patch.object(service.asyncio, "create_subprocess_exec", return_value=process) as create,
        patch.object(service, "_terminate_process_group", new_callable=AsyncMock) as cleanup,
    ):
        with pytest.raises(service.CrawlTaskError, match="超时"):
            await service._run_mediacrawler(service.PLATFORM_SPECS["xiaohongshu"], "AI", 20)
        cleanup.assert_awaited_once_with(process)
        assert create.call_args.args[:3] == ("uv", "run", "--frozen")
        assert create.call_args.kwargs["env"]["TZ"] == "Asia/Shanghai"
        assert create.call_args.kwargs["start_new_session"] is True
        assert create.call_args.kwargs["limit"] >= 1024 * 1024


@pytest.mark.asyncio
async def test_login_failure_is_not_a_successful_empty_crawl(monkeypatch):
    async def output():
        yield b"[XiaoHongShuLogin.login_by_qrcode] Login xiaohongshu failed by qrcode login method ...\n"

    process = AsyncMock()
    process.pid = 12345
    process.stdout = output()
    process.wait.return_value = 0
    with (
        patch.object(service.asyncio, "create_subprocess_exec", return_value=process),
        patch.object(service, "_terminate_process_group", new_callable=AsyncMock),
    ):
        with pytest.raises(service.CrawlTaskError, match="登录"):
            await service._run_mediacrawler(service.PLATFORM_SPECS["xiaohongshu"], "AI", 20)


@pytest.mark.asyncio
async def test_task_reaps_its_browser_after_crawl_failure(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    task = SimpleNamespace(source_platform="xiaohongshu", keyword="AI", limit_count=20, status="pending")
    db = Mock()
    db.get = AsyncMock(return_value=task)
    db.commit = AsyncMock()

    @asynccontextmanager
    async def session():
        yield db

    browser = Mock()
    monkeypatch.setattr(service, "AsyncSessionLocal", session)
    monkeypatch.setattr(service, "_read_existing_ids", lambda spec: set())
    monkeypatch.setattr(service, "_ensure_debug_chrome_running", AsyncMock(return_value=browser))
    monkeypatch.setattr(
        service, "_run_mediacrawler", AsyncMock(side_effect=service.CrawlTaskError("登录失败"))
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(service, "_terminate_process_group", cleanup)
    await service._run_single_platform_task("test")
    assert task.status == "failed"
    cleanup.assert_awaited_once_with(browser)


@pytest.mark.asyncio
async def test_import_obeys_requested_small_batch_limit(monkeypatch, tmp_path):
    import json
    from unittest.mock import Mock

    data = tmp_path / "notes.jsonl"
    data.write_text(
        "\n".join(json.dumps({"note_id": str(i), "title": "AI", "desc": "提示词"}) for i in range(5))
    )
    monkeypatch.setattr(service, "_jsonl_path_for_today", lambda spec: data)
    monkeypatch.setattr(service, "_local_media_paths", lambda spec, note_id: [])
    db = Mock()
    db.execute = AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=None)))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    assert (
        await service._import_jsonl_to_drafts(db, service.PLATFORM_SPECS["xiaohongshu"], set(), limit=3) == 3
    )
    assert db.add.call_count == 3


@pytest.mark.asyncio
async def test_large_real_subprocess_log_does_not_abort_crawl(monkeypatch):
    import sys

    create_original = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        return await create_original(
            sys.executable,
            "-c",
            "print('x' * 200000)",
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            limit=kwargs["limit"],
            start_new_session=True,
        )

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", create)
    code, log = await service._run_mediacrawler(service.PLATFORM_SPECS["xiaohongshu"], "AI", 3)
    assert code == 0
    assert len(log) == 8000
