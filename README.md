fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree.
Use '--' to separate paths from revisions, like this:
'git <command> [<revision>...] -- [<file>...]'
# AIGC提示词站 · 后端服务（aigc-prompt-backend）

> 这是 [AIGC提示词站](https://aigc-prompt.com)（图片类 AI 提示词展示站）的后端服务代码。
> 产品背景、功能需求、技术选型的完整决策过程记录在 `docs/` 目录，建议开发前先读一遍。

---

## 一、这是什么项目

一个免费的图片类 AI 提示词展示站：用户浏览别人用即梦/可灵/Midjourney 等模型跑出来的效果图，一键复制对应的提示词，自己拿去生成。参考了海外的 Timi AI、PromptBase，但做了国内化改造（分类、模型标签、登录方式等）。

MVP 阶段**不考虑盈利**，目标是先验证产品形态、把日活做到几百级别。

---

## 二、文档地图（新接手这个项目，按这个顺序读）

| 顺序 | 文档 | 作用 |
|---|---|---|
| 1 | `AGENT.md` | **AI 协作留痕**，记录了项目的历史决策、踩过的坑、下一步该做什么。换模型接手必读。 |
| 2 | `docs/aigc-prompt-调研与前置准备.md` | 为什么做这个产品、否掉了哪些方案（交易市场模式等） |
| 3 | `docs/aigc-prompt-PRD.md` | 完整产品需求文档：功能点、原型图、优先级 |
| 4 | `docs/aigc-prompt-后端技术选型.md` | 技术栈选择理由、数据库设计、API 设计、部署方案 |
| 5 | `PROGRESS.md` | **开发进度留痕**，按功能模块记录完成情况，开发中随时更新 |
| 6 | 本文件（README.md） | 怎么把项目跑起来 |

---

## 三、技术栈

- **语言**：Python 3.13
- **Web 框架**：FastAPI（异步）
- **数据库**：PostgreSQL 16 + SQLAlchemy 2.0（异步）+ Alembic（迁移）
- **缓存**：Redis 7
- **认证**：JWT + Google OAuth2（主） + 邮箱验证码（辅）
- **图片存储**：阿里云 OSS + CDN
- **依赖管理**：uv
- **代码规范**：ruff（lint）
- **测试**：pytest + pytest-asyncio

详细选型理由见 `docs/aigc-prompt-后端技术选型.md`。

---

## 四、项目结构

```
aigc-prompt-backend/
├── app/
│   ├── main.py              # FastAPI 应用入口，组装所有路由
│   ├── config.py            # 环境变量配置（Pydantic Settings）
│   ├── database.py          # 数据库连接（异步 SQLAlchemy）
│   ├── redis.py             # Redis 连接
│   ├── scheduler.py         # 定时任务（浏览量回写、热门搜索词聚合）
│   ├── models/              # ORM 模型（数据库表结构）
│   ├── schemas/              # Pydantic Schema（请求/响应结构）
│   ├── routers/              # FastAPI 路由（HTTP 接口定义）
│   ├── services/             # 业务逻辑层（路由层调这里，不直接写 SQL）
│   └── utils/                 # 工具函数（JWT、Google 验证、OSS、认证依赖）
├── alembic/                   # 数据库迁移脚本
├── tests/                     # 单元测试
├── docs/                       # 产品/技术决策文档（从工作区同步而来）
├── admin-panel/                 # 后台管理面板（纯静态HTML+JS，运营方自用，和产品主站无关）
├── dev-notes/                   # 开发过程中的零散笔记（可选）
├── docker-compose.yml           # 本地一键启动 Postgres + Redis + API
├── Dockerfile
├── .env.example                  # 环境变量示例，复制为 .env 后填真实值
├── AGENT.md                       # AI 协作留痕文档
└── PROGRESS.md                     # 开发进度文档
```

每个目录的职责边界：**models 定义表结构，schemas 定义接口的输入输出格式，routers 只做参数解析和调用 services，services 才是真正写数据库查询和业务规则的地方**。新增功能时，应该先想清楚这四层各自要加什么，而不是把逻辑一股脑写进 router。

---

## 五、本地启动

### 5.1 准备工作

1. 安装 [uv](https://github.com/astral-sh/uv)（Python 包管理工具）
2. 安装 Docker（用于启动本地 Postgres 和 Redis）
3. 复制环境变量文件：
   ```bash
   cp .env.example .env
   ```
   然后把 `.env` 里的 `GOOGLE_CLIENT_ID`、`RESEND_API_KEY`、`OSS_*` 等真实密钥填进去（本地纯跑接口测试，这几个可以先留空，涉及到的功能会报错，但不影响其他接口）。

### 5.2 启动数据库和缓存

```bash
docker compose up -d postgres redis
```

### 5.3 安装依赖

```bash
uv sync
```

### 5.4 执行数据库迁移

```bash
uv run alembic upgrade head
```

> 迁移脚本 `alembic/versions/ff6f26c6fa2d_init_tables.py` 已经**真实生成并真实执行验证过**（本机用 Homebrew 安装的 PostgreSQL 16 测试通过，建出全部 7 张表），直接 `upgrade head` 即可，不需要重新 `autogenerate`。

### 5.5 启动服务

```bash
uv run uvicorn app.main:app --reload --port 8000
```

启动后访问 http://localhost:8000/docs 查看自动生成的 API 文档（Swagger UI），可以直接在页面上测试每个接口。

### 5.6 一键全部启动（推荐）

```bash
docker compose up --build
```

---

## 六、给前端 / 其他 Agent 的接口对接说明

前端是独立开发（可能是别人或别的 AI Agent），**不需要手写接口文档，直接用 FastAPI 自动生成的 OpenAPI 规范对接**：

| 方式 | 地址（服务跑起来后访问） | 用途 |
|---|---|---|
| Swagger UI | `http://localhost:8000/docs` | 交互式文档，能直接在页面上发请求测试，人类对接首选 |
| ReDoc | `http://localhost:8000/redoc` | 更美观的只读文档 |
| OpenAPI JSON | `http://localhost:8000/openapi.json` | 机器可读规范，AI Agent / 代码生成工具（如 `openapi-generator`、`openapi-typescript`）首选，能自动生成请求代码和 TypeScript 类型定义 |

本仓库 `docs/openapi.json` 保存了一份**已验证真实可用**的规范快照（截至写入时，23 个接口全部跑通，详见 `PROGRESS.md`）。注意：这是某次导出的快照，接口有变动后记得重新导出覆盖（服务启动中执行 `curl http://localhost:8000/openapi.json -o docs/openapi.json`）。

当前已实现的接口清单（均已真实验证通过，详见 `PROGRESS.md`）：
- 提示词：首页列表/筛选/排序、搜索、详情、相似推荐、浏览量记录、点赞、收藏、举报
- 认证：邮箱验证码登录（发码/核验）、Google 登录、获取当前用户
- 用户：我的收藏列表
- 分类：分类列表（含计数）
- 搜索：热门搜索词、记录搜索日志
- **后台管理**（`/api/admin/*`，需要在请求头带 `X-Admin-Key`，不是面向普通用户的接口）：内容的增删改查+发布/下架、举报列表与处理（可联动下架）

---

## 七、运行测试和代码检查

```bash
# 跑单元测试
uv run pytest -v

# 代码风格检查
uv run ruff check .

# 自动修复可修复的风格问题
uv run ruff check . --fix
```

---

## 八、给接手这个项目的人（人类或 AI）的建议

1. **先读 AGENT.md**，了解项目的决策历史，别重复犯过去已经讨论清楚的错误（比如内容来源的版权风险评估、技术选型的理由）。
2. **新增功能前先看 PRD**（`docs/aigc-prompt-PRD.md`），确认功能点的优先级（P0/P1/P2），不要自己随意加功能或者改变优先级。
3. **每完成一个功能模块，去 PROGRESS.md 补一条记录**，格式见该文件开头的说明。
4. **数据库表结构变化必须用 Alembic 迁移**，不要手动改数据库或者绕过迁移机制。
5. **新增 ORM 模型后，记得去 `app/models/__init__.py` 补充 import**，否则 Alembic 的 autogenerate 识别不到新表。
