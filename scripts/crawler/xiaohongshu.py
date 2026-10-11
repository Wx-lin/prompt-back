"""
小红书笔记采集脚本（离线运行，非后端服务的一部分）。

技术路线（见 docs/aigc-prompt-爬虫方案.md 2.1 节）：
**浏览器自动化 + 复用登录态**，不自己逆向小红书的签名算法（x-s / x-s-common）。
用 Playwright 控制真实浏览器，登录一次后把 Cookie 持久化到本地，后续运行直接复用；
所有请求都通过真实浏览器页面发出，平台前端自己会算好签名参数，脚本不需要关心。

用法：
    # 第一次运行：会弹出浏览器窗口，需要手动扫码登录小红书（建议用小号）
    uv run python scripts/crawler/xiaohongshu.py login

    # 按关键词采集（会打开浏览器真实搜索）
    uv run python scripts/crawler/xiaohongshu.py search --keyword "AI头像提示词" --limit 5

    # 采集单篇指定笔记（已知笔记ID或完整URL）
    uv run python scripts/crawler/xiaohongshu.py note --url "https://www.xiaohongshu.com/explore/xxxxx"

风险提示（务必理解）：登录态采集需要用真实账号，账号被平台限制/封禁的风险由使用者
自行承担。建议用小号、保持低频（脚本内置了请求间隔）、不要用主账号。
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

# 保证脚本能 import 到项目根目录的 app 包和同目录的其他脚本
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BROWSER_STATE_DIR, CRAWL_MEDIA_DIR, REQUEST_DELAY_SECONDS  # noqa: E402
from storage import draft_exists, save_draft  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402

XHS_BASE = "https://www.xiaohongshu.com"

# Playwright 默认的浏览器 UA 会被小红书识别并返回风控拦截页（"IP存在风险，请切换
# 可靠网络环境后重试"），真实测试验证过：换成常见的真实 Chrome UA 后可以正常访问。
# 这不是绕过登录验证，只是避免被当成明显的自动化脚本流量直接拒之门外。
REAL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------- 登录态管理 ----------


async def login_flow(wait_seconds: int = 120) -> None:
    """
    打开浏览器让用户手动扫码登录，登录后把登录态（Cookie + localStorage）持久化到本地。

    不依赖终端交互输入（input()）：改成轮询检测页面上是否出现登录后才会有的元素
    （用户头像/发布按钮），每 3 秒检查一次，最多等 wait_seconds 秒。检测到登录
    成功就立即保存状态退出；超时则提示失败，但仍然会尝试保存当前状态（可能是
    半成品，建议超时后重新运行并确保在窗口里真的完成了扫码）。
    """
    BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = BROWSER_STATE_DIR / "xiaohongshu_state.json"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(user_agent=REAL_USER_AGENT)
        page = await context.new_page()
        await page.goto(XHS_BASE)

        print("\n" + "=" * 60)
        print("浏览器已打开小红书首页（窗口会出现在你的屏幕上）。")
        print("请在弹出的浏览器窗口里手动扫码登录（建议用小号，不要用主账号）。")
        print(f"脚本会自动检测登录状态，最多等待 {wait_seconds} 秒。")
        print("=" * 60 + "\n")

        logged_in = False
        elapsed = 0
        check_interval = 3
        while elapsed < wait_seconds:
            await asyncio.sleep(check_interval)
            elapsed += check_interval
            # 用 cookie 判断登录状态比用 DOM 选择器更可靠：登录成功后小红书会
            # 下发 web_session 这个 cookie，这是登录态的本质标志，不会因为页面
            # 改版（class 名变化）而失效。真实测试验证过：未登录时没有这个 cookie。
            cookies = await context.cookies()
            has_session = any(c["name"] == "web_session" for c in cookies)
            if has_session:
                logged_in = True
                print(f"检测到登录成功（耗时 {elapsed} 秒）")
                break
            print(f"  等待登录中...（已等待 {elapsed}/{wait_seconds} 秒）")

        await context.storage_state(path=str(state_path))
        await browser.close()

    if logged_in:
        print(f"\n✅ 登录态已保存到 {state_path}")
        print("后续运行采集命令会直接复用这个登录态，不需要重复登录。")
    else:
        print(f"\n⚠️ 超时未检测到登录成功，但已尝试保存当前状态到 {state_path}")
        print("如果确实已经在窗口里完成了扫码，可以先尝试跑 search/note 命令看能否正常采集；")
        print("如果提示未登录，说明保存的状态不完整，需要重新运行 login 命令。")


async def _new_context(p: "async_playwright") -> tuple[BrowserContext, Page]:
    """创建一个复用了登录态的浏览器上下文；没有登录态则提示先跑 login。"""
    state_path = BROWSER_STATE_DIR / "xiaohongshu_state.json"
    if not state_path.exists():
        raise RuntimeError("未找到登录态文件，请先运行：uv run python scripts/crawler/xiaohongshu.py login")

    browser = await p.chromium.launch(headless=False)  # 保持有头模式，便于观察和应对验证码
    context = await browser.new_context(storage_state=str(state_path), user_agent=REAL_USER_AGENT)
    page = await context.new_page()
    return context, page


# ---------- 笔记内容解析 ----------


def _extract_note_id(url: str) -> str:
    """从笔记 URL 里提取笔记ID，用于去重。支持 /explore/<id> 和 /discovery/item/<id> 两种形式。"""
    match = re.search(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)", url)
    if not match:
        raise ValueError(f"无法从 URL 中识别笔记ID：{url}")
    return match.group(1)


async def _download_images(page: Page, image_urls: list[str], note_id: str) -> list[str]:
    """
    下载笔记配图到本地 crawl_media 目录，返回相对路径列表（存进数据库用相对路径，
    后端拼接 /local-media/ 前缀对外提供访问）。

    用 Playwright 的 request 上下文下载（复用浏览器的 Cookie 和 UA），比直接用
    httpx/requests 更不容易被图片CDN拦截。
    """
    note_dir = CRAWL_MEDIA_DIR / "xiaohongshu" / note_id
    note_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for index, url in enumerate(image_urls):
        try:
            response = await page.request.get(url)
            if not response.ok:
                print(f"  图片 {index} 下载失败（HTTP {response.status}），跳过")
                continue
            body = await response.body()
            # 从 URL 里推断扩展名，失败了就用 .jpg 兜底
            ext_match = re.search(r"\.(jpg|jpeg|png|webp)", url, re.IGNORECASE)
            ext = ext_match.group(1) if ext_match else "jpg"
            file_name = f"{index}.{ext}"
            (note_dir / file_name).write_bytes(body)
            saved.append(f"xiaohongshu/{note_id}/{file_name}")
            print(f"  图片 {index} 已保存：{file_name}")
        except Exception as exc:  # noqa: BLE001 - 单张图片失败不应该中断整条笔记的采集
            print(f"  图片 {index} 下载异常，跳过：{exc}")

    return saved


async def _parse_note_detail(page: Page) -> dict:
    """
    从已打开的笔记详情页解析出标题、正文、作者、配图URL。

    用页面内 JS 直接从 DOM 里取文本（比解析 HTML 字符串更稳），这些选择器基于
    小红书当前（2026年）的页面结构，平台改版后可能需要跟着调整——这也是浏览器
    自动化方案比逆向签名方案更好维护的地方：改版通常只是改几个选择器。
    """
    data = await page.evaluate("""() => {
        const titleEl = document.querySelector('#detail-title') || document.querySelector('.title');
        const descEl = document.querySelector('#detail-desc') || document.querySelector('.desc');
        const authorEl = document.querySelector('.author-wrapper .username')
            || document.querySelector('.username');
        const imgs = Array.from(document.querySelectorAll('.swiper-slide img, .note-slider img'))
            .map(img => img.src)
            .filter(src => src && !src.startsWith('data:'));

        return {
            title: titleEl ? titleEl.innerText.trim() : null,
            content: descEl ? descEl.innerText.trim() : '',
            author: authorEl ? authorEl.innerText.trim() : null,
            images: [...new Set(imgs)],
        };
    }""")
    return data


async def crawl_single_note(page: Page, url: str) -> dict | None:
    """采集单篇笔记，返回解析出的数据字典（不含入库），失败返回 None。"""
    note_id = _extract_note_id(url)
    print(f"正在打开笔记：{url}")
    await page.goto(url, wait_until="domcontentloaded")
    # 等待详情内容渲染出来，最多等 10 秒
    try:
        await page.wait_for_selector("#detail-desc, .desc", timeout=10000)
    except Exception:  # noqa: BLE001
        print("  页面未能加载出笔记正文（可能需要登录、笔记已删除、或遇到验证码）")
        return None

    await asyncio.sleep(1)  # 留一点时间给图片懒加载
    data = await _parse_note_detail(page)
    if not data["content"]:
        print("  未解析到正文内容，跳过")
        return None

    data["note_id"] = note_id
    data["url"] = url
    return data


async def _save_note(page: Page, data: dict) -> bool:
    """把解析出的笔记数据（含下载图片）写入数据库草稿表。已存在则跳过。"""
    async with AsyncSessionLocal() as db:
        if await draft_exists(db, "xiaohongshu", data["note_id"]):
            print(f"  笔记 {data['note_id']} 已采集过，跳过")
            return False

        local_images = await _download_images(page, data["images"], data["note_id"])
        await save_draft(
            db,
            source_platform="xiaohongshu",
            source_note_id=data["note_id"],
            source_url=data["url"],
            raw_title=data["title"],
            raw_content=data["content"],
            author_name=data["author"],
            local_image_paths=local_images,
        )
    print(f"  ✅ 已写入草稿表：{data['title'] or data['note_id']}")
    return True


# ---------- 命令行入口 ----------


async def cmd_search(keyword: str, limit: int) -> None:
    """按关键词搜索并采集前 N 篇笔记（跳过已采集过的）。"""
    async with async_playwright() as p:
        context, page = await _new_context(p)
        print(f'正在搜索关键词：{keyword}')
        await page.goto(f"{XHS_BASE}/search_result?keyword={keyword}&type=51")
        await asyncio.sleep(3)

        # 从搜索结果页收集笔记链接
        links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]'))
                .map(a => a.href.split('?')[0])
                .filter(href => href.includes('/explore/'));
        }""")
        links = list(dict.fromkeys(links))[:limit]  # 去重并限制数量
        print(f"找到 {len(links)} 篇候选笔记")

        saved_count = 0
        for index, link in enumerate(links):
            print(f"\n[{index + 1}/{len(links)}]")
            data = await crawl_single_note(page, link)
            if data and await _save_note(page, data):
                saved_count += 1
            await asyncio.sleep(REQUEST_DELAY_SECONDS)  # 低频采集，降低风控风险

        await context.browser.close()
        print(f"\n采集完成，本次新增 {saved_count} 条草稿。")


async def cmd_note(url: str) -> None:
    """采集单篇指定笔记。"""
    async with async_playwright() as p:
        context, page = await _new_context(p)
        data = await crawl_single_note(page, url)
        if data:
            await _save_note(page, data)
        await context.browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书笔记采集脚本")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("login", help="扫码登录并保存登录态")

    search_parser = subparsers.add_parser("search", help="按关键词搜索采集")
    search_parser.add_argument("--keyword", required=True, help="搜索关键词")
    search_parser.add_argument("--limit", type=int, default=5, help="最多采集多少篇（默认5）")

    note_parser = subparsers.add_parser("note", help="采集单篇笔记")
    note_parser.add_argument("--url", required=True, help="笔记完整URL")

    args = parser.parse_args()

    if args.command == "login":
        asyncio.run(login_flow())
    elif args.command == "search":
        asyncio.run(cmd_search(args.keyword, args.limit))
    elif args.command == "note":
        asyncio.run(cmd_note(args.url))


if __name__ == "__main__":
    main()
