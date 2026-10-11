import uuid

import pytest

from app.services import crawl_login_service as login


def test_private_task_metadata_and_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "_directory", lambda: tmp_path)
    task = uuid.uuid4()
    assert login.get_login_metadata(task)["state"] == "unavailable"
    with pytest.raises(ValueError):
        login.request_login_refresh(task)
    login.write_login_metadata(task, "awaiting_login", "扫码")
    assert login.get_login_metadata(task)["task_id"] == str(task)
    assert login.get_login_metadata(task)["expires_at"]
    login.request_login_refresh(task)
    assert (tmp_path / f"{task}.refresh").exists()
    login.write_login_metadata(task, "authenticated", "已登录")
    assert login.get_login_metadata(task)["expires_at"] is None


def test_next_slot_shanghai_and_strict_future():
    from datetime import UTC, datetime

    from scripts.crawl_dispatch import compute_next_run

    now = datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
    assert compute_next_run(["10:00", "22:00"], now) == datetime(2026, 10, 10, 14, tzinfo=UTC)


def test_hidden_qr_is_not_login_required():
    import asyncio
    from unittest.mock import AsyncMock, Mock

    page = Mock()
    candidates = Mock()
    candidates.count = AsyncMock(return_value=2)
    hidden = Mock(is_visible=AsyncMock(return_value=False))
    visible = Mock(is_visible=AsyncMock(return_value=True))
    candidates.nth.side_effect = [hidden, visible]
    page.locator.return_value = candidates
    assert asyncio.run(login.visible_login_qr(page)) is visible
    candidates.nth.side_effect = [hidden, hidden]
    assert asyncio.run(login.visible_login_qr(page)) is None


def test_identical_qr_preserves_expiry_and_new_qr_changes_version(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(login, "_directory", lambda: tmp_path)
    task = uuid.uuid4()
    login.save_login_qr(task, b"first-image")
    first = login.get_login_metadata(task)
    assert first["qr_version"]
    path = tmp_path / f"{task}.json"
    first["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(first))
    login.save_login_qr(task, b"first-image")
    assert login.get_login_metadata(task)["expires_at"] == first["expires_at"]
    login.save_login_qr(task, b"new-image")
    assert login.get_login_metadata(task)["qr_version"] != first["qr_version"]


def test_observer_selects_latest_page_not_stale_login_tab():
    from types import SimpleNamespace

    old = SimpleNamespace(url="https://www.xiaohongshu.com/", is_closed=lambda: False)
    current = SimpleNamespace(url="https://www.xiaohongshu.com/explore", is_closed=lambda: False)
    browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[old, current])])
    assert login.select_login_page(browser, "xiaohongshu")[1] is current


def test_refresh_waits_for_browser_and_publishes_new_version(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    monkeypatch.setattr(login, "_directory", lambda: tmp_path)
    task = uuid.uuid4()
    login.save_login_qr(task, b"old")
    old = login.get_login_metadata(task)["qr_version"]
    login.request_login_refresh(task)
    qr = Mock(screenshot=AsyncMock(return_value=b"new"))
    monkeypatch.setattr(login, "visible_login_qr", AsyncMock(return_value=qr))
    monkeypatch.setattr(login, "_record_phase", AsyncMock())
    page = Mock(url="https://www.xiaohongshu.com/explore")
    fresh_page = Mock(goto=AsyncMock())
    context = Mock(new_page=AsyncMock(return_value=fresh_page))
    asyncio.run(login._observe_page(task, context, page))
    fresh_page.goto.assert_awaited_once()
    qr.screenshot.assert_not_awaited()
    asyncio.run(login._observe_page(task, Mock(), page))
    assert login.get_login_metadata(task)["qr_version"] != old
    assert login.login_image_path(task).read_bytes() == b"new"
    assert login.login_image_path(task).stat().st_mode & 0o777 == 0o600


def test_authenticated_sidebar_saves_private_state(tmp_path, monkeypatch):
    import asyncio
    import json
    from unittest.mock import AsyncMock, Mock

    folder = tmp_path / "login"
    folder.mkdir()
    monkeypatch.setattr(login, "_directory", lambda: folder)
    monkeypatch.setattr(login, "visible_login_qr", AsyncMock(return_value=None))
    phase = AsyncMock()
    monkeypatch.setattr(login, "_record_phase", phase)
    me = Mock(count=AsyncMock(return_value=1), first=Mock(is_visible=AsyncMock(return_value=True)))
    page = Mock(locator=Mock(return_value=me))
    state = {"cookies": [{"name": "sample", "value": "never-expose"}], "origins": []}
    context = Mock(storage_state=AsyncMock(return_value=state))
    task = uuid.uuid4()
    asyncio.run(login._observe_page(task, context, page))
    assert login.get_login_metadata(task)["state"] == "authenticated"
    assert json.loads((tmp_path / "storage_state.json").read_text()) == state
    assert (tmp_path / "storage_state.json").stat().st_mode & 0o777 == 0o600
    phase.assert_awaited_once_with(task, False)
