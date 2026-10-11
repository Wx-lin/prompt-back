"""Task-specific private login artifacts; never expose browser credentials."""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CrawlTask

logger = logging.getLogger(__name__)


def _directory():
    path = Path(get_settings().mediacrawler_dir).expanduser() / ".chrome_debug_profile" / "login"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def login_image_path(task_id):
    return _directory() / f"{uuid.UUID(str(task_id))}.png"


def get_login_metadata(task_id):
    path = login_image_path(task_id).with_suffix(".json")
    try:
        value = json.loads(path.read_text())
        if isinstance(value, dict) and value.get("task_id") == str(task_id):
            return value
    except (OSError, ValueError):
        pass
    return dict(
        task_id=str(task_id), state="unavailable", message="当前无需扫码", expires_at=None, qr_version=None
    )


def _private_write(path, contents):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(contents)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def write_login_metadata(task_id, state, message, qr_version=None):
    previous = get_login_metadata(task_id)
    expires_at = None
    if state == "awaiting_login":
        same = qr_version is not None and qr_version == previous.get("qr_version")
        expires_at = (
            previous.get("expires_at") if same else (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        )
    value = dict(
        task_id=str(task_id), state=state, message=message, expires_at=expires_at, qr_version=qr_version
    )
    _private_write(
        login_image_path(task_id).with_suffix(".json"), json.dumps(value, ensure_ascii=False).encode()
    )


def save_login_qr(task_id, image, platform="xiaohongshu"):
    version = hashlib.sha256(image).hexdigest()
    previous = get_login_metadata(task_id)
    if version != previous.get("qr_version"):
        _private_write(login_image_path(task_id), image)
    label = {"xiaohongshu": "小红书", "weibo": "微博", "kuaishou": "快手"}.get(platform, platform)
    write_login_metadata(task_id, "awaiting_login", f"请使用{label} App 扫码并确认登录", version)


def request_login_refresh(task_id):
    if get_login_metadata(task_id)["state"] != "awaiting_login":
        raise ValueError("当前任务不在等待扫码")
    login_image_path(task_id).with_suffix(".refresh").touch(mode=0o600)


async def visible_login_qr(page, platform="xiaohongshu"):
    if platform == "weibo" and urlparse(page.url).hostname == "passport.weibo.com":
        candidates = page.locator("img.w-full.h-full")
    else:
        candidates = page.locator('img.qrcode-img, div.qrcode-img img, .qrcode img, canvas[class*="qrcode"]')
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if await candidate.is_visible():
            return candidate
    return None


def select_login_page(browser, platform):
    domain = {"xiaohongshu": "xiaohongshu.com", "weibo": "weibo.com", "kuaishou": "kuaishou.com"}.get(
        platform
    )
    if domain:
        for context in reversed(browser.contexts):
            for page in reversed(context.pages):
                host = urlparse(page.url).hostname or ""
                if not page.is_closed() and (host == domain or host.endswith("." + domain)):
                    return context, page
    return None


async def _record_phase(task_id, waiting):
    async with AsyncSessionLocal() as db:
        task = await db.get(CrawlTask, task_id)
        if task and task.status in ("running", "awaiting_login"):
            task.status = "awaiting_login" if waiting else "running"
            task.stage = "等待扫码" if waiting else "采集中"
            await db.commit()


async def _observe_page(task_id, context, page, platform="xiaohongshu"):
    # The original initial tab may still show an obsolete login dialog. Only the crawler's newest tab matters.
    qr = await visible_login_qr(page, platform)
    if qr is not None:
        marker = login_image_path(task_id).with_suffix(".refresh")
        if marker.exists():
            marker.unlink()
            fresh_page = await context.new_page()
            await fresh_page.goto(page.url, wait_until="domcontentloaded", timeout=30000)
            return
        save_login_qr(task_id, await qr.screenshot(), platform)
        await _record_phase(task_id, True)
        return
    me = page.locator("xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']")
    if await me.count() and await me.first.is_visible():
        write_login_metadata(task_id, "authenticated", "登录成功，正在继续采集")
        state = await context.storage_state()
        _private_write(_directory().parent / "storage_state.json", json.dumps(state).encode())
        await _record_phase(task_id, False)


async def observe_login(task_id, platform, stop):
    if platform not in ("xiaohongshu", "weibo", "kuaishou"):
        return
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
            while not stop.is_set():
                try:
                    selected = select_login_page(browser, platform)
                    if selected:
                        await _observe_page(task_id, *selected, platform=platform)
                except Exception as exc:
                    logger.warning("Login observer for task %s: %s", task_id, type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
    except Exception as exc:
        logger.warning("Login observer could not connect for task %s: %s", task_id, type(exc).__name__)
        write_login_metadata(task_id, "unavailable", "登录页面读取失败，请稍后重试")
