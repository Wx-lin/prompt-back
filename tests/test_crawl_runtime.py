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
async def test_access_denied_is_reported_without_requesting_another_scan():
    async def output():
        yield b"PlatformAccessError: XHS_LOGIN_ACCESS_DENIED code=-104\n"

    process = AsyncMock()
    process.stdout = output()
    process.wait.return_value = 1
    with (
        patch.object(service.asyncio, "create_subprocess_exec", return_value=process),
        patch.object(service, "_terminate_process_group", new_callable=AsyncMock),
    ):
        with pytest.raises(service.CrawlTaskError, match="平台拒绝.*-104"):
            await service._run_mediacrawler(service.PLATFORM_SPECS["xiaohongshu"], "AI", 1)


@pytest.mark.asyncio
async def test_task_reaps_its_browser_after_crawl_failure(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    task = SimpleNamespace(source_platform="xiaohongshu", keyword="AI", limit_count=20, status="pending")
    db = Mock()
    db.get = AsyncMock(return_value=task)
    db.commit = AsyncMock()
    db.execute = AsyncMock(return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[])))))

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


def test_extract_avatar_url_accepts_only_http_urls():
    assert service._extract_avatar_url({"author_avatar_url": " https://example.com/a.jpg "}) == "https://example.com/a.jpg"
    assert service._extract_avatar_url({"author_avatar_url": "javascript:alert(1)"}) is None
    assert service._extract_avatar_url({"author_avatar_url": 42}) is None


@pytest.mark.asyncio
async def test_import_preserves_author_avatar(monkeypatch, tmp_path):
    import json
    from unittest.mock import Mock

    data = tmp_path / "notes.jsonl"
    data.write_text(
        json.dumps(
            {
                "note_id": "n1",
                "desc": "prompt",
                "author_avatar_url": "https://example.com/avatar.jpg",
            }
        )
    )
    monkeypatch.setattr(service, "_jsonl_path_for_today", lambda spec: data)
    monkeypatch.setattr(service, "_local_media_paths", lambda spec, note_id: [])
    db = Mock()
    db.execute = AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=None)))
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    assert await service._import_jsonl_to_drafts(db, service.PLATFORM_SPECS["xiaohongshu"], set()) == 1
    assert db.add.call_args.args[0].author_avatar_url == "https://example.com/avatar.jpg"


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


@pytest.mark.asyncio
async def test_failed_process_does_not_wait_for_stuck_login_observer(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import crawl_login_service

    task = SimpleNamespace(
        source_platform="xiaohongshu", keyword="AI", limit_count=1, status="pending", stage=""
    )
    db = Mock(get=AsyncMock(return_value=task), commit=AsyncMock())
    db.execute = AsyncMock(
        return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=["previous-day"]))))
    )

    @asynccontextmanager
    async def session():
        yield db

    async def observer(*args):
        task.stage = "等待扫码"
        await asyncio.Event().wait()

    async def failed_crawl(*args):
        await asyncio.sleep(0)
        return 1, "browser failed"

    monkeypatch.setattr(service, "AsyncSessionLocal", session)
    monkeypatch.setattr(service, "_read_existing_ids", lambda spec: set())
    monkeypatch.setattr(service, "_ensure_debug_chrome_running", AsyncMock(return_value=None))
    monkeypatch.setattr(crawl_login_service, "observe_login", observer)
    monkeypatch.setattr(service, "_run_mediacrawler", failed_crawl)
    await asyncio.wait_for(service._run_single_platform_task("test"), timeout=4)
    assert task.status == "failed"
    assert task.stage == "失败"


@pytest.mark.asyncio
async def test_browser_close_timeout_terminates_owned_process(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import playwright.async_api

    async def stuck(*args, **kwargs):
        await asyncio.Event().wait()

    cdp = Mock(send=stuck)
    browser = Mock(new_browser_cdp_session=AsyncMock(return_value=cdp))
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(
        chromium=Mock(connect_over_cdp=AsyncMock(return_value=browser))
    )
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: manager)
    monkeypatch.setattr(service, "BROWSER_CLOSE_TIMEOUT_SECONDS", 0.01, raising=False)
    process = Mock(wait=AsyncMock())
    terminate = AsyncMock()
    monkeypatch.setattr(service, "_terminate_process_group", terminate)
    await asyncio.wait_for(service._close_owned_browser(process), timeout=0.2)
    terminate.assert_awaited_once_with(process)


@pytest.mark.asyncio
@pytest.mark.parametrize("await_review", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_success_is_only_recorded_after_browser_cleanup(monkeypatch, await_review, cleanup_fails):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import crawl_login_service

    task = SimpleNamespace(
        source_platform="xiaohongshu", keyword="AI", limit_count=20, status="pending", finished_at=None
    )
    db = Mock(get=AsyncMock(return_value=task), commit=AsyncMock())
    db.execute = AsyncMock(
        return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=["previous-day"]))))
    )

    @asynccontextmanager
    async def session():
        yield db

    async def cleanup(process):
        assert task.status == "running"
        assert task.stage == "采集收尾中"
        assert task.finished_at is None
        assert task.new_draft_count == 18
        if cleanup_fails:
            raise TimeoutError("browser did not exit")

    monkeypatch.setattr(service, "AsyncSessionLocal", session)
    monkeypatch.setattr(service, "_read_existing_ids", lambda spec: {"today"})
    monkeypatch.setattr(service, "_ensure_debug_chrome_running", AsyncMock(return_value=Mock()))
    monkeypatch.setattr(crawl_login_service, "observe_login", AsyncMock())
    monkeypatch.setattr(service, "_run_mediacrawler", AsyncMock(return_value=(0, "ok")))
    monkeypatch.setattr(service, "_import_jsonl_to_drafts", AsyncMock(return_value=18))
    monkeypatch.setattr(service, "_close_owned_browser", cleanup)
    await service._run_single_platform_task("task", await_review=await_review)
    service._run_mediacrawler.assert_awaited_once_with(
        service.PLATFORM_SPECS["xiaohongshu"], "AI", 20, excluded_ids={"previous-day", "today"}
    )
    if cleanup_fails:
        assert task.status == "failed"
        assert task.stage == "采集收尾失败"
        assert task.finished_at is not None
    else:
        assert task.status == ("reviewing" if await_review else "success")
        assert (task.finished_at is None) is await_review


@pytest.mark.asyncio
@pytest.mark.parametrize("spawn_fails", [False, True])
async def test_xhs_exclusion_snapshot_private_and_removed(spawn_fails):
    import json
    from pathlib import Path

    captured = []

    async def spawn(*args, **kwargs):
        path = Path(kwargs["env"]["AIGC_XHS_EXCLUDE_IDS_FILE"])
        captured.append(path)
        assert json.loads(path.read_text()) == ["old-a", "old-b"]
        assert path.stat().st_mode & 0o777 == 0o600
        if spawn_fails:
            raise OSError("spawn failed")
        async def output():
            yield b'AIGC_XHS_SEARCH_SUMMARY {}\n'
        process = AsyncMock()
        process.stdout = output()
        process.wait.return_value = 0
        return process

    with (
        patch.object(service.asyncio, "create_subprocess_exec", side_effect=spawn),
        patch.object(service, "_terminate_process_group", new_callable=AsyncMock),
    ):
        if spawn_fails:
            with pytest.raises(OSError):
                await service._run_mediacrawler(
                    service.PLATFORM_SPECS["xiaohongshu"], "AI", 2, excluded_ids={"old-a", "old-b"}
                )
        else:
            await service._run_mediacrawler(
                service.PLATFORM_SPECS["xiaohongshu"], "AI", 2, excluded_ids={"old-a", "old-b"}
            )
    assert captured and not captured[0].exists()


@pytest.mark.asyncio
async def test_manual_comma_keywords_preserve_legacy_search_mode():
    async def output():
        yield b"finished\n"

    process = AsyncMock()
    process.stdout = output()
    process.wait.return_value = 0
    with (
        patch.object(service.asyncio, "create_subprocess_exec", return_value=process) as spawn,
        patch.object(service, "_terminate_process_group", new_callable=AsyncMock),
    ):
        await service._run_mediacrawler(
            service.PLATFORM_SPECS["xiaohongshu"], "AI portrait,AI scene", 20,
            excluded_ids={"old"},
        )
    assert "AIGC_XHS_EXCLUDE_IDS_FILE" not in spawn.call_args.kwargs["env"]
    assert "AI portrait,AI scene" in spawn.call_args.args
