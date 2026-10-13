"""
采集任务管理：后台"点击采集"的核心逻辑，负责拉起 MediaCrawler 子进程并在完成后
把采集结果导入 crawl_drafts 草稿表。

多平台支持：一次"开始采集"可以勾选多个平台（小红书/微博/快手），会创建一批
CrawlTask（一个平台一条，batch_id 相同），按顺序排队执行——同一时间只有一个
MediaCrawler 子进程在跑，因为它们共享同一个 CDP 调试 Chrome 实例，并发跑会
相互干扰页面状态。

平台数据形态差异（这是必须如实处理的限制，不能假装三个平台体验一致）：
- 小红书：图文笔记，jsonl 的 image_list 字段直接给出图片 CDN URL，无需额外下载。
- 微博：图文动态，但 jsonl 存储的 content_item 里没有图片 URL 字段（MediaCrawler
  只有在 ENABLE_GET_MEDIA=True 时才会把图片下载到本地 data/wb/media/{note_id}/）。
- 快手：主要是短视频，jsonl 里只有封面图 video_cover_url，没有图集；
  ENABLE_GET_MEDIA=True 时会下载封面到本地 data/ks/media/{video_id}/。
  （快手內容形态和"AI提示词+效果图"不太匹配，采集到的草稿多数只有一张封面图，
  需要在后台页面上明确提示这一点，不能让运营方误以为快手和小红书一样丰富。）
所以这里对三个平台一律开启 --get_media，统一通过扫描本地媒体目录拿图片，
不依赖 jsonl 字段差异，保证三个平台的图片导入逻辑一致、可维护。

设计取舍（为什么不用 Celery/RQ 这类任务队列）：
- 采集任务量很小（运营方手动点击触发，不会高并发），用 asyncio 子进程 + 内存状态
  跟踪就够用，不需要引入消息队列这套基础设施。
- 但任务是真实的"后台异步工作"：HTTP 请求创建任务后立即返回，子进程在后台跑，
  前端轮询 GET /api/admin/crawl-tasks/{id} 看状态——这是任务队列系统要解决的
  核心问题的最小实现版本。
- 进程重启后正在运行的任务状态会丢失（变成"卡在 running"），这是当前方案的已知
  局限：运营方手动点击的场景下，重启后台服务本来就不频繁，可以接受。

与 MediaCrawler 的集成方式：
- MediaCrawler 是独立的第三方项目（NON-COMMERCIAL LEARNING LICENSE 1.1 许可），
  这里只是通过 subprocess 调用它的 main.py，不 import 它的任何代码，避免许可证
  边界模糊。
- MediaCrawler 用 CDP 模式连接一个专用的 Chrome 调试 profile（不是用户日常 Chrome，
  因为 Chrome 不允许在默认 profile 上开远程调试端口），采集前必须确保这个 Chrome
  进程在运行且已登录过对应平台（三个平台的登录态互相独立，分别在这个调试浏览器
  里访问对应网站登录一次）。
"""

import asyncio
import atexit
import json
import os
import signal
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CrawlDraft, CrawlDraftImage, CrawlTask

settings = get_settings()

# MediaCrawler 项目目录（独立于本项目，和 aigc-prompt-backend 平级）
MEDIACRAWLER_DIR = Path(settings.mediacrawler_dir).expanduser().resolve()
CHROME_DEBUG_PROFILE_DIR = MEDIACRAWLER_DIR / ".chrome_debug_profile"
CDP_DEBUG_PORT = 9222

CHROME_BINARY_CANDIDATES = [
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


@dataclass(frozen=True)
class PlatformSpec:
    """
    一个平台的采集参数适配：MediaCrawler 的 CLI 代号、jsonl/media 落盘的目录名、
    jsonl 里标识这条记录的字段名、落地到草稿表的平台标签、展示给运营方的说明。

    注意 jsonl_dir_name 和 media_dir_name 不是同一个值——这是 MediaCrawler 自身
    不一致的地方（已通过真实运行验证）：
    - jsonl 落盘用的是完整平台名：data/{xhs|weibo|kuaishou}/jsonl/...
      （见 store/{xhs|weibo|kuaishou}/_store_impl.py 的 AsyncFileWriter(platform=...)）
    - 媒体下载落盘用的是 CLI 简码：data/{xhs|wb|ks}/media/{content_id}/...
      （见 media_platform/{xhs|weibo|kuaishou}/core.py 的 MediaDownloader(platform=...)）
    必须分别适配，否则会出现"采集进程正常跑完、图片也下载成功，但扫描 jsonl 目录
    什么都读不到"的静默失败（新增条数为 0 但不报错）。
    """

    cli_code: str  # 传给 main.py --platform 的值
    jsonl_dir_name: str  # data/{jsonl_dir_name}/jsonl/{type}_contents_YYYY-MM-DD.jsonl
    media_dir_name: str  # data/{media_dir_name}/media/{content_id}/
    id_field: str  # jsonl 记录里代表帖子 ID 的字段名
    source_platform: str  # 存入 crawl_drafts.source_platform 的值
    note_url_template: str  # 兜底的笔记链接模板（jsonl 里没给完整链接时用）
    content_warning: str | None  # 该平台数据形态的已知局限，供前端展示给运营方


PLATFORM_SPECS: dict[str, PlatformSpec] = {
    "xiaohongshu": PlatformSpec(
        cli_code="xhs",
        jsonl_dir_name="xhs",
        media_dir_name="xhs",
        id_field="note_id",
        source_platform="xiaohongshu",
        note_url_template="https://www.xiaohongshu.com/explore/{id}",
        content_warning=None,
    ),
    "weibo": PlatformSpec(
        cli_code="wb",
        jsonl_dir_name="weibo",
        media_dir_name="wb",
        id_field="note_id",
        source_platform="weibo",
        note_url_template="https://m.weibo.cn/detail/{id}",
        content_warning=None,
    ),
    "kuaishou": PlatformSpec(
        cli_code="ks",
        jsonl_dir_name="kuaishou",
        media_dir_name="ks",
        id_field="video_id",
        source_platform="kuaishou",
        note_url_template="https://www.kuaishou.com/short-video/{id}",
        content_warning="快手内容以短视频为主，草稿通常只有一张视频封面图，不像小红书有多张效果图，审核时请注意甄别。",
    ),
}


class CrawlTaskError(Exception):
    """采集任务失败时抛出，message 面向运营方可读。"""

    def __init__(self, message, log_tail=""):
        super().__init__(message)
        self.log_tail = log_tail


def _chrome_binary() -> str:
    candidates = [settings.chrome_binary] if settings.chrome_binary else CHROME_BINARY_CANDIDATES
    binary = next((path for path in candidates if Path(path).is_file()), None)
    if binary is None:
        raise CrawlTaskError("未找到 Chrome，请安装 Chromium 或配置 CHROME_BINARY")
    return binary


def _signal_process_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Reap both the leader and any descendant processes."""
    _signal_process_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        pass
    finally:
        _signal_process_group(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), timeout=5)


async def _ensure_debug_chrome_running() -> asyncio.subprocess.Process | None:
    """
    确保调试专用 Chrome（带 --remote-debugging-port）正在运行。

    不自动帮用户登录——如果这个 profile 从没登录过某个平台，MediaCrawler 子进程里
    的登录检测会失败，错误信息会体现在 log_tail 里，运营方需要手动在这个 Chrome
    窗口里打开对应平台网站扫码/登录一次（登录态会永久保存在这个独立 profile 里，
    三个平台的登录态互不影响，可以依次登录）。
    """
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-sS",
        "-m",
        "3",
        f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and stdout:
        return  # 已经在运行，直接复用

    chrome_binary = _chrome_binary()

    CHROME_DEBUG_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    chrome = await asyncio.create_subprocess_exec(
        chrome_binary,
        f"--remote-debugging-port={CDP_DEBUG_PORT}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={CHROME_DEBUG_PROFILE_DIR}",
        "--no-first-run",
        "--disable-features=WebUIOmniboxPopup",
        *(["--headless=new", "--disable-dev-shm-usage"] if settings.chrome_headless else []),
        "https://www.xiaohongshu.com",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    # Container must use init: true to reap descendants on signal shutdown.
    atexit.register(_signal_process_group, chrome.pid, signal.SIGTERM)

    for _ in range(10):
        await asyncio.sleep(1)
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-sS",
            "-m",
            "2",
            f"http://127.0.0.1:{CDP_DEBUG_PORT}/json/version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            await _restore_browser_state()
            return chrome

    await _terminate_process_group(chrome)
    raise CrawlTaskError("启动调试用 Chrome 失败，请检查 Chrome 是否安装正常")


async def _restore_browser_state():
    state_path = CHROME_DEBUG_PROFILE_DIR / "storage_state.json"
    if not state_path.exists():
        return
    import json

    from playwright.async_api import async_playwright

    try:
        state = json.loads(state_path.read_text())
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_DEBUG_PORT}")
            context = browser.contexts[0]
            await context.add_cookies(state.get("cookies", []))
            # Populate each origin only on that origin; do not expose values to logs.
            origins = state.get("origins", [])
            await context.add_init_script(
                """(() => {
                const origins = """
                + json.dumps(origins)
                + """;
                for (const entry of origins) {
                    if (entry.origin === location.origin) {
                        for (const item of entry.localStorage || []) {
                            localStorage.setItem(item.name, item.value);
                        }
                    }
                }
            })();"""
            )
    except Exception:
        return


BROWSER_CLOSE_TIMEOUT_SECONDS = 5


async def _close_owned_browser(process):
    async def graceful_close():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_DEBUG_PORT}")
            session = await browser.new_browser_cdp_session()
            await session.send("Browser.close")
        await process.wait()

    try:
        # CDP may never acknowledge Browser.close after Chrome begins exiting.
        # Bound the entire connection/command/driver shutdown, not just process.wait.
        await asyncio.wait_for(graceful_close(), timeout=BROWSER_CLOSE_TIMEOUT_SECONDS)
    except Exception:
        await _terminate_process_group(process)


def _jsonl_path_for_today(spec: PlatformSpec) -> Path:
    """
    MediaCrawler 按日期生成 jsonl 文件名：data/{jsonl_dir_name}/jsonl/search_contents_YYYY-MM-DD.jsonl

    文件名固定是 "{crawler_type}_{item_type}_{date}"：crawler_type 对应 CLI --type
    参数（这里固定传 search），item_type 固定是 contents（见 AsyncFileWriter._get_file_path）。
    """
    today = datetime.now(ZoneInfo(settings.crawl_timezone)).strftime("%Y-%m-%d")
    return MEDIACRAWLER_DIR / "data" / spec.jsonl_dir_name / "jsonl" / f"search_contents_{today}.jsonl"


def _media_dir(spec: PlatformSpec, content_id: str) -> Path:
    """MediaCrawler 媒体下载目录：data/{media_dir_name}/media/{content_id}/，见 media_downloader/paths.py"""
    return MEDIACRAWLER_DIR / "data" / spec.media_dir_name / "media" / content_id


def _read_existing_ids(spec: PlatformSpec) -> set[str]:
    """读取当前 jsonl 文件里已有的记录 ID，用作"采集前快照"（同一天多次采集会追加写同一个文件）。"""
    import json

    jsonl_path = _jsonl_path_for_today(spec)
    if not jsonl_path.exists():
        return set()
    ids: set[str] = set()
    with jsonl_path.open(encoding="utf-8") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                item = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            record_id = item.get(spec.id_field)
            if record_id:
                ids.add(str(record_id))
    return ids


async def _run_mediacrawler(
    spec: PlatformSpec, keyword: str, limit: int, *, excluded_ids: set[str] | None = None
) -> tuple[int, str]:
    env = {**os.environ, "TZ": settings.crawl_timezone}
    env.pop("AIGC_XHS_EXCLUDE_IDS_FILE", None)
    # Legacy manual input may contain comma-separated search terms.
    if spec.source_platform != "xiaohongshu" or excluded_ids is None or "," in keyword:
        return await _run_mediacrawler_process(spec, keyword, limit, env)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as snapshot:
        json.dump(sorted(excluded_ids), snapshot)
        snapshot.flush()
        return await _run_mediacrawler_process(
            spec, keyword, limit, {**env, "AIGC_XHS_EXCLUDE_IDS_FILE": snapshot.name}
        )


async def _run_mediacrawler_process(
    spec: PlatformSpec, keyword: str, limit: int, env: dict[str, str]
) -> tuple[int, str]:
    """
    用子进程跑 MediaCrawler 的 main.py，返回 (returncode, 日志尾部文本)。

    关键词/数量/平台/是否下载媒体全部通过 CLI 参数传递，不再改写 MediaCrawler 的
    配置文件——CLI 参数的覆盖优先级更明确，也避免多个平台任务排队执行时互相覆盖
    配置文件内容的竞态问题。
    """
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "--frozen",
        "--no-sync",
        "main.py",
        "--platform",
        spec.cli_code,
        "--lt",
        "qrcode",
        "--type",
        "search",
        "--keywords",
        keyword,
        "--crawler_max_notes_count",
        str(limit),
        "--get_media",
        "true",  # 统一开启媒体下载，三个平台的图片都走"扫描本地媒体目录"这一条路径
        "--save_data_option",
        "jsonl",
        "--headless",
        str(settings.chrome_headless).lower(),
        cwd=str(MEDIACRAWLER_DIR),
        env=env,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=4 * 1024 * 1024,
    )

    log_lines: list[str] = []

    async def collect_output() -> int:
        assert process.stdout is not None
        async for raw_line in process.stdout:
            log_lines.append(raw_line.decode("utf-8", errors="replace").rstrip()[:8000])
            if len(log_lines) > 200:
                del log_lines[:-200]
        return await process.wait()

    try:
        returncode = await asyncio.wait_for(collect_output(), settings.crawl_timeout_seconds)
        log_tail = "\n".join(log_lines[-80:])
        if "XHS_LOGIN_ACCESS_DENIED" in log_tail:
            raise CrawlTaskError(
                "小红书平台拒绝当前账号或会话访问（-104）：您当前登录的账号没有权限访问。"
                "本次已停止采集；请先在小红书确认账号状态，重复扫码不保证恢复。",
                log_tail,
            )
        if any(
            marker in log_tail
            for marker in (
                "Login xiaohongshu failed",
                "Login weibo failed",
                "Login kuaishou failed",
                "login failed , have not found qrcode",
            )
        ):
            raise CrawlTaskError("扫码登录未完成或平台不认可登录会话", log_tail)
        if returncode != 0:
            if "300012" in log_tail:
                raise CrawlTaskError("平台限制服务器 IP（300012），暂时无法采集", log_tail)
            if "captcha" in log_tail.lower() or "请通过验证" in log_tail:
                raise CrawlTaskError("平台要求人工安全验证，请在登录页面完成验证后重试", log_tail)
        return returncode, log_tail
    except TimeoutError as exc:
        raise CrawlTaskError(
            f"采集超时（{settings.crawl_timeout_seconds} 秒），已停止进程", "\n".join(log_lines[-80:])
        ) from exc
    finally:
        await _terminate_process_group(process)


def _local_media_paths(spec: PlatformSpec, content_id: str) -> list[str]:
    """
    扫描 MediaCrawler 为某条内容下载的本地媒体文件，返回相对于 MEDIACRAWLER_DIR/data
    的路径列表（这个基准和 app/main.py 挂载的 /mediacrawler-media 静态路由对齐，
    路由层拼接 /mediacrawler-media/ 前缀就能直接访问，不需要再处理路径转换）。

    为什么扫描目录而不是解析 jsonl 字段：微博/快手的 jsonl content_item 里没有
    图片 URL 字段（见本文件顶部说明），只能靠 ENABLE_GET_MEDIA 落盘后反过来扫描
    目录拿到文件。小红书其实 jsonl 里也有 image_list，但为了三个平台统一处理
    逻辑、少维护一套分支，也统一走扫描目录这条路。
    """
    media_dir = _media_dir(spec, content_id)
    if not media_dir.exists():
        return []
    # 只收图片（视频平台的封面也是图片格式），不收 .mp4，草稿审核页面不需要放视频
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(p for p in media_dir.iterdir() if p.suffix.lower() in image_extensions)
    data_root = MEDIACRAWLER_DIR / "data"
    return [str(p.relative_to(data_root)) for p in files]


async def _import_jsonl_to_drafts(
    db: AsyncSession, spec: PlatformSpec, ids_before: set[str], limit: int | None = None
) -> int:
    """
    把本次新采集的 jsonl 记录导入 crawl_drafts 草稿表，返回新增条数。

    三个平台共用这一个函数，字段读取通过 spec 适配，但三个平台的 jsonl 结构
    并不完全一致（字段名不同），所以标题/正文/作者字段按平台分别取值。
    """
    import json

    jsonl_path = _jsonl_path_for_today(spec)
    if not jsonl_path.exists():
        return 0

    new_count = 0
    with jsonl_path.open(encoding="utf-8") as f:
        for raw_line in f:
            if limit is not None and new_count >= limit:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            item = json.loads(raw_line)
            record_id = item.get(spec.id_field)
            if not record_id:
                continue
            record_id = str(record_id)
            if record_id in ids_before:
                continue  # 之前已经读取过的旧记录，跳过

            exists_query = select(CrawlDraft.id).where(
                CrawlDraft.source_platform == spec.source_platform,
                CrawlDraft.source_note_id == record_id,
            )
            if (await db.execute(exists_query)).scalar_one_or_none() is not None:
                continue

            title, content, author, note_url = _extract_fields(spec, item, record_id)
            local_paths = _local_media_paths(spec, record_id)

            draft = CrawlDraft(
                source_platform=spec.source_platform,
                source_note_id=record_id,
                source_url=note_url,
                raw_title=title,
                raw_content=content,
                author_name=author,
                author_avatar_url=_extract_avatar_url(item),
                status="pending",
            )
            db.add(draft)
            await db.flush()

            for index, local_path in enumerate(local_paths):
                db.add(CrawlDraftImage(draft_id=draft.id, local_path=local_path, sort_order=index))

            new_count += 1

    await db.commit()
    return new_count


def _extract_fields(
    spec: PlatformSpec, item: dict, record_id: str
) -> tuple[str | None, str, str | None, str]:
    """
    从一条 jsonl 记录里按平台取出 (标题, 正文, 作者, 链接)。

    三个平台的 store 层字段名不同（见 MediaCrawler 的 store/xhs|weibo|kuaishou/__init__.py）：
    - 小红书：title / desc / nickname / note_url
    - 微博：没有独立 title，用 content 的前 60 字当标题；content / nickname / note_url
    - 快手：title（其实是 caption）/ desc / nickname / video_url
    """
    if spec.source_platform == "xiaohongshu":
        title = item.get("title") or None
        content = item.get("desc") or ""
        author = item.get("nickname") or None
        note_url = item.get("note_url") or spec.note_url_template.format(id=record_id)
    elif spec.source_platform == "weibo":
        content = item.get("content") or ""
        title = (content[:60] + "…") if len(content) > 60 else (content or None)
        author = item.get("nickname") or None
        note_url = item.get("note_url") or spec.note_url_template.format(id=record_id)
    else:  # kuaishou
        title = item.get("title") or None
        content = item.get("desc") or ""
        author = item.get("nickname") or None
        note_url = item.get("video_url") or spec.note_url_template.format(id=record_id)

    return title, content, author, note_url


def _extract_avatar_url(item: dict) -> str | None:
    """Use the avatar field emitted by the platform store, ignoring unsafe or invalid URLs."""
    value = item.get("author_avatar_url") or item.get("avatar_url")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 1000 or not value.startswith(("https://", "http://")):
        return None
    return value


async def _run_single_platform_task(task_id: uuid.UUID, *, await_review: bool = False) -> None:
    """执行单个平台任务：拉起 MediaCrawler → 导入草稿 → 更新任务状态。"""
    async with AsyncSessionLocal() as db:
        task = await db.get(CrawlTask, task_id)
        if task is None:
            return

        spec = PLATFORM_SPECS.get(task.source_platform)
        if spec is None:
            task.status = "failed"
            task.error_message = f"不支持的平台：{task.source_platform}"
            task.finished_at = datetime.now(UTC)
            await db.commit()
            return

        task.status = "running"
        task.stage = "采集中"
        await db.commit()

        owned_chrome = None
        crawl_completed = False
        try:
            ids_before = _read_existing_ids(spec)
            excluded_ids = None
            if spec.source_platform == "xiaohongshu":
                known = await db.execute(select(CrawlDraft.source_note_id).where(
                    CrawlDraft.source_platform == spec.source_platform
                ))
                excluded_ids = ids_before | {str(note_id) for note_id in known.scalars().all()}
            owned_chrome = await _ensure_debug_chrome_running()
            from app.services.crawl_login_service import observe_login

            stop_observer = asyncio.Event()
            observer = asyncio.create_task(observe_login(task_id, task.source_platform, stop_observer))
            try:
                returncode, log_tail = await _run_mediacrawler(
                    spec, task.keyword, task.limit_count, excluded_ids=excluded_ids
                )
            finally:
                stop_observer.set()
                # A browser read may be stuck after the crawler has already exited.
                # Give login persistence a short grace period, then cancel the observer.
                try:
                    await asyncio.wait_for(observer, timeout=2)
                except TimeoutError:
                    pass

            if returncode != 0:
                task.status = "failed"
                task.stage = "失败"
                task.error_message = "采集进程异常退出，详见日志"
                task.log_tail = log_tail
                task.finished_at = datetime.now(UTC)
                await db.commit()
                return

            new_count = await _import_jsonl_to_drafts(db, spec, ids_before, limit=task.limit_count)

            task.status = "running"
            task.stage = "采集收尾中"
            task.new_draft_count = new_count
            task.log_tail = log_tail
            task.finished_at = None
            await db.commit()
            crawl_completed = True

        except CrawlTaskError as exc:
            task.status = "failed"
            task.stage = "失败"
            task.error_message = str(exc)
            task.log_tail = exc.log_tail
            task.finished_at = datetime.now(UTC)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - 任务执行体必须兜住所有异常
            task.status = "failed"
            task.stage = "失败"
            task.error_message = f"未预期的错误：{exc}"
            task.finished_at = datetime.now(UTC)
            await db.commit()
        finally:
            if owned_chrome is not None:
                try:
                    await _close_owned_browser(owned_chrome)
                except Exception as exc:
                    crawl_completed = False
                    task.status = "failed"
                    task.stage = "采集收尾失败"
                    task.error_message = f"浏览器清理失败：{type(exc).__name__}"
                    task.finished_at = datetime.now(UTC)
                    await db.commit()
                else:
                    atexit.unregister(_signal_process_group)

        if crawl_completed:
            task.status = "reviewing" if await_review else "success"
            task.stage = "AI 审核中" if await_review else "采集完成"
            task.finished_at = None if await_review else datetime.now(UTC)
            await db.commit()


async def _run_batch(task_ids: list[uuid.UUID]) -> None:
    """
    依次执行一批任务（排队，不并行）。

    排队而不是并行的原因：所有平台共享同一个 CDP 调试 Chrome 实例，并行跑多个
    MediaCrawler 子进程会同时抢这一个浏览器的页面控制权，导致互相干扰甚至采集
    到串台的数据，所以必须串行。
    """
    from app.services.crawl_lock import crawl_lock

    async with crawl_lock() as acquired:
        if acquired:
            for task_id in task_ids:
                await _run_single_platform_task(task_id)
            return
        async with AsyncSessionLocal() as db:
            for task_id in task_ids:
                task = await db.get(CrawlTask, task_id)
                if task is not None:
                    task.status = "failed"
                    task.error_message = "已有采集任务正在运行，请稍后重试"
                    task.finished_at = datetime.now(UTC)
            await db.commit()


async def start_crawl_batch(
    db: AsyncSession, platforms: list[str], keyword: str, limit: int
) -> list[CrawlTask]:
    """
    开始一批采集任务：为每个勾选的平台创建一条 CrawlTask（status=pending），
    共享同一个 batch_id，然后在后台按顺序依次执行，立即返回这批任务记录。

    调用方应该把返回的任务列表全部给前端，前端对每个 task.id 分别轮询状态。
    """
    unknown = [p for p in platforms if p not in PLATFORM_SPECS]
    if unknown:
        raise ValueError(f"不支持的平台：{', '.join(unknown)}")

    batch_id = uuid.uuid4()
    tasks: list[CrawlTask] = []
    for platform in platforms:
        task = CrawlTask(
            batch_id=batch_id,
            source_platform=platform,
            keyword=keyword,
            limit_count=limit,
            status="pending",
        )
        db.add(task)
        tasks.append(task)

    await db.commit()
    for task in tasks:
        await db.refresh(task)

    # fire-and-forget：故意不 await，让它在后台排队跑，HTTP 请求立即返回
    asyncio.create_task(_run_batch([t.id for t in tasks]))

    return tasks


async def list_crawl_tasks(db: AsyncSession, page: int, page_size: int) -> tuple[list[CrawlTask], int]:
    """任务历史列表，按创建时间倒序。"""
    from sqlalchemy import func as sa_func

    total = (await db.execute(select(sa_func.count()).select_from(CrawlTask))).scalar_one()
    query = (
        select(CrawlTask)
        .order_by(CrawlTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await db.execute(query)).scalars().all())
    return rows, total


async def get_crawl_task(db: AsyncSession, task_id: uuid.UUID) -> CrawlTask | None:
    return await db.get(CrawlTask, task_id)


async def list_tasks_by_batch(db: AsyncSession, batch_id: uuid.UUID) -> list[CrawlTask]:
    """按 batch_id 查询一批任务的全部状态，前端可以用这个一次性拿到多平台进度。"""
    query = select(CrawlTask).where(CrawlTask.batch_id == batch_id).order_by(CrawlTask.created_at)
    return list((await db.execute(query)).scalars().all())


def get_platform_warnings() -> dict[str, str]:
    """返回有已知内容局限的平台及其说明文案，供前端在勾选平台时提示运营方。"""
    return {key: spec.content_warning for key, spec in PLATFORM_SPECS.items() if spec.content_warning}
