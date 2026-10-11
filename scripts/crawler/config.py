"""
爬虫脚本的公共配置和工具。

说明：这是一个**离线脚本**，不属于 FastAPI 后端服务（app/ 目录）的一部分，不会随
Web 服务启动，需要手动运行。它直接连数据库、直接写 crawl_drafts 表，不经过 HTTP
接口——因为采集这件事涉及浏览器登录态、人工审核判断，不适合做成常驻服务。

相关文档：docs/aigc-prompt-爬虫方案.md
"""

import os
import sys
from pathlib import Path

# 让脚本能 import 到项目根目录下的 app 包（脚本不在 app/ 内部，需要在 sys.path 里
# 补上项目根目录，否则从 scripts/crawler/ 这个位置跑起来 import app.xxx 会失败）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 采集到的图片先存本地（阿里云 OSS 密钥还没配置），目录和后端配置保持一致，
# 后端会把这个目录挂到 /local-media 路径下对外提供访问（见 app/main.py）。
CRAWL_MEDIA_DIR = PROJECT_ROOT / os.getenv("CRAWL_MEDIA_DIR", "./crawl_media")

# Playwright 的登录态持久化目录：第一次运行需要扫码登录，登录后 Cookie 存在这里，
# 后续运行直接复用，不用每次登录。这个目录不要提交到 git（已在 .gitignore 排除）。
BROWSER_STATE_DIR = PROJECT_ROOT / ".crawler_state"

# 请求间隔（秒），避免请求过快被平台风控识别。个人开发者手动小批量采集，
# 不需要追求速度，宁可慢一点降低被封风险（见方案文档第四节风险提示）。
REQUEST_DELAY_SECONDS = 5.0
