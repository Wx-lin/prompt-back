# AIGC提示词站（aigc-prompt.com）· 后端技术选型与方案

> 版本：v1.0　日期：2026-10-08
> 对应 PRD：《aigc-prompt-PRD.md》v1.1

---

## 一、技术栈总览

| 层次 | 选型 | 说明 |
|---|---|---|
| 语言 | **Python 3.11+** | 生态成熟，FastAPI 原生支持 |
| Web 框架 | **FastAPI** | 异步、自动生成 OpenAPI 文档、性能好 |
| 数据库 | **PostgreSQL** | 关系型，支持全文检索，后续扩展性好 |
| ORM | **SQLAlchemy 2.0 + Alembic** | 异步支持，迁移管理 |
| 缓存 | **Redis** | 热门/浏览量排序缓存、限流、邮件验证码存储 |
| 图片存储 | **阿里云 OSS（或腾讯云 COS）** | 国内 CDN 加速，适合面向国内用户 |
| 邮件发送 | **Resend API（或阿里云邮件推送）** | 用于邮箱验证码登录 |
| 认证 | **JWT + Google OAuth2** | Google 登录为主，邮箱验证码为辅 |
| 依赖管理 | **uv** | 快速、现代的 Python 包管理 |
| 部署 | **Docker + Docker Compose** | 容器化，便于本地开发和云端部署 |
| 云服务器 | **阿里云 ECS（轻量型）** | 国内备案需要国内服务器 |

---

## 二、数据库设计

### 2.1 核心表结构

**prompts（提示词主表）**
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
title           VARCHAR(200) NOT NULL          -- 标题
prompt_text     TEXT NOT NULL                  -- 完整提示词原文
description     TEXT                           -- 使用说明（变量说明等）
category        VARCHAR(50) NOT NULL           -- 分类（head_avatar/portrait/poster/...）
models          TEXT[]                         -- 适用模型（即梦/可灵/Midjourney 等，数组）
author_name     VARCHAR(100)                   -- 来源作者名
source_url      VARCHAR(500)                   -- 来源链接（小红书/抖音/X）
is_published    BOOLEAN DEFAULT FALSE          -- 是否已发布
view_count      INTEGER DEFAULT 0             -- 浏览量
like_count      INTEGER DEFAULT 0             -- 点赞数
collect_count   INTEGER DEFAULT 0             -- 收藏数
created_at      TIMESTAMPTZ DEFAULT NOW()
updated_at      TIMESTAMPTZ DEFAULT NOW()
```

**prompt_images（效果图，一对多）**
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
prompt_id       UUID REFERENCES prompts(id) ON DELETE CASCADE
image_url       VARCHAR(500) NOT NULL          -- OSS 图片 URL
thumbnail_url   VARCHAR(500)                   -- 缩略图 URL（OSS 处理生成）
sort_order      INTEGER DEFAULT 0             -- 多图排序（第几张）
created_at      TIMESTAMPTZ DEFAULT NOW()
```

**users（用户表）**
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
email           VARCHAR(200) UNIQUE            -- 邮箱（邮箱登录时必填）
google_id       VARCHAR(100) UNIQUE            -- Google 用户 ID（Google 登录时必填）
avatar_url      VARCHAR(500)                   -- 头像（来自 Google 账号或默认）
nickname        VARCHAR(100)                   -- 昵称
is_active       BOOLEAN DEFAULT TRUE
created_at      TIMESTAMPTZ DEFAULT NOW()
last_login_at   TIMESTAMPTZ
```

**user_likes（点赞记录）**
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id         UUID REFERENCES users(id) ON DELETE CASCADE
prompt_id       UUID REFERENCES prompts(id) ON DELETE CASCADE
created_at      TIMESTAMPTZ DEFAULT NOW()
UNIQUE(user_id, prompt_id)                     -- 每人每条只能点赞一次
```

**user_collects（收藏记录）**
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id         UUID REFERENCES users(id) ON DELETE CASCADE
prompt_id       UUID REFERENCES prompts(id) ON DELETE CASCADE
created_at      TIMESTAMPTZ DEFAULT NOW()
UNIQUE(user_id, prompt_id)
```

**search_logs（搜索日志，用于统计热门搜索词）**
```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
keyword         VARCHAR(100) NOT NULL          -- 搜索关键词（原文，不做归一化处理）
user_id         UUID REFERENCES users(id)      -- 可为空（匿名搜索）
created_at      TIMESTAMPTZ DEFAULT NOW()
```

> 热门搜索排行通过定时任务（如每小时跑一次）聚合 `search_logs` 近 7 天的 `keyword` 出现频次计算排名，写入 Redis 缓存，`trend` 字段通过对比本次排名和上一次排名的升降得出（上升→up，下降→down，不变→flat）。该表只做日志记录，不需要为每次搜索做实时聚合查询。

---

### 2.2 索引设计

```sql
-- 首页瀑布流排序
CREATE INDEX idx_prompts_created_at ON prompts(created_at DESC) WHERE is_published = TRUE;
CREATE INDEX idx_prompts_like_count ON prompts(like_count DESC) WHERE is_published = TRUE;
CREATE INDEX idx_prompts_view_count ON prompts(view_count DESC) WHERE is_published = TRUE;

-- 分类筛选
CREATE INDEX idx_prompts_category ON prompts(category) WHERE is_published = TRUE;

-- 模型标签（数组字段用 GIN 索引）
CREATE INDEX idx_prompts_models ON prompts USING GIN(models);

-- 全文搜索（title + description）
CREATE INDEX idx_prompts_fulltext ON prompts
  USING GIN(to_tsvector('simple', title || ' ' || COALESCE(description, '')));
```

---

## 三、API 设计

### 3.1 接口清单

**提示词相关**

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/prompts` | 首页瀑布流，支持分类/排序/分页 |
| GET | `/api/prompts/:id` | 获取单条详情（弹窗详情 + SEO 直达页用） |
| GET | `/api/prompts/:id/similar` | 获取相似内容（同分类，排除自身） |
| GET | `/api/prompts/search` | 关键词搜索 |
| GET | `/api/search/trending` | 热门搜索词列表（搜索弹窗用，含趋势标记） |
| POST | `/api/prompts/:id/view` | 记录浏览（匿名也可，用于浏览量统计） |
| POST | `/api/prompts/:id/like` | 点赞（需登录） |
| DELETE | `/api/prompts/:id/like` | 取消点赞（需登录） |
| POST | `/api/prompts/:id/collect` | 收藏（需登录） |
| DELETE | `/api/prompts/:id/collect` | 取消收藏（需登录） |

**用户与认证**

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/auth/send-code` | 发送邮箱验证码 |
| POST | `/api/auth/verify-code` | 验证验证码，返回 JWT |
| POST | `/api/auth/google` | Google OAuth 回调，返回 JWT |
| GET | `/api/auth/me` | 获取当前用户信息（需登录） |
| GET | `/api/users/me/collects` | 获取我的收藏列表（需登录） |

**分类**

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/categories` | 获取分类列表（含每个分类的内容数量） |

---

### 3.2 GET /api/prompts 参数说明

```
category    string    分类过滤（head_avatar/portrait/poster/...），不传则全部
models      string[]  模型标签过滤（多选，OR 逻辑）
sort        string    排序方式：latest（默认）/ popular（点赞）/ hot（浏览量）
page        integer   页码，默认 1
page_size   integer   每页数量，默认 20，最大 50
```

**Response**（分页瀑布流数据）：
```json
{
  "items": [
    {
      "id": "uuid",
      "title": "赛博朋克风头像",
      "category": "head_avatar",
      "models": ["即梦", "Midjourney"],
      "thumbnail_url": "https://cdn.../thumb_xxx.jpg",
      "like_count": 128,
      "view_count": 3400,
      "author_name": "@xxx",
      "created_at": "2026-10-01T10:00:00Z"
    }
  ],
  "total": 256,
  "page": 1,
  "page_size": 20,
  "has_more": true
}
```

### 3.3 搜索弹窗相关接口（新增，对应"搜索提示词、风格或场景"弹窗设计）

**GET /api/search/trending**

获取热门搜索词列表，用于搜索弹窗打开时默认展示的"热门搜索"区域。

**Response**：
```json
{
  "items": [
    { "keyword": "自然光人像", "rank": 1, "trend": "up" },
    { "keyword": "极简产品摄影", "rank": 2, "trend": "flat" },
    { "keyword": "治愈系插画", "rank": 3, "trend": "up" },
    { "keyword": "电影感街拍", "rank": 4, "trend": "flat" }
  ]
}
```

- `trend` 字段（up/down/flat）用于前端展示箭头图标（参考截图里"自然光人像"行的 ↗ 图标）
- 后端通过统计 `search_logs` 表最近 N 天的搜索频次计算排行，缓存到 Redis，定时（如每小时）刷新

**POST /api/search/log**（搜索日志记录，供统计热门词使用）

```json
{ "keyword": "胶片人像" }
```

每次用户在搜索框发起一次实际搜索（按 Enter 或点击搜索结果），前端调用一次此接口记录，用于后续计算热门搜索排行。匿名也可调用（不需要登录）。

> **"最近搜索"（最近搜索的"胶片人像""电商产品图"等标签）不需要后端支持**，纯前端用 localStorage 存储当前用户最近搜索过的关键词（建议存最近 10 条），"清空"按钮直接清 localStorage。因为这是**个人维度**的历史记录，不需要跨设备同步，也不需要登录，纯本地存储即可，不额外设计后端接口和表。

---

## 四、认证方案

### 4.1 流程

**Google 登录**：
```
前端 → Google OAuth 弹窗 → Google 返回 id_token
→ POST /api/auth/google { id_token }
→ 后端验证 id_token（google-auth-library）
→ 查找或创建 user 记录
→ 返回自签 JWT
→ 前端存入 localStorage / Cookie
```

**邮箱验证码登录**：
```
POST /api/auth/send-code { email }
→ 生成 6 位验证码，存入 Redis（key: verify:{email}，TTL: 10 分钟）
→ 调用邮件服务发送验证码邮件

POST /api/auth/verify-code { email, code }
→ 从 Redis 取出验证码对比
→ 验证成功 → 查找或创建 user 记录 → 返回 JWT
→ 删除 Redis 中的验证码（防止重用）
```

### 4.2 JWT 设计

```python
payload = {
    "sub": str(user.id),       # 用户 UUID
    "email": user.email,
    "exp": datetime.utcnow() + timedelta(days=30),  # 30 天有效期
    "iat": datetime.utcnow()
}
```

---

## 五、图片存储与 CDN

### 5.1 存储方案

- 使用**阿里云 OSS**存储原图和缩略图
- 缩略图用 OSS 图片处理生成（`?x-oss-process=image/resize,w_400`）
- 通过阿里云 CDN 分发，加速国内访问

### 5.2 上传流程（MVP 阶段，运营者手动录入）

```
本地图片 → aliyun-oss-sdk 上传到 OSS → 获得图片 URL
→ 手动将 URL 写入 prompt_images 表
```

> 后期如开放用户上传，增加前端直传 OSS 的预签名 URL 流程，避免图片流量走后端服务器。

---

## 六、缓存策略

| 缓存对象 | Key 设计 | TTL | 说明 |
|---|---|---|---|
| 首页热门列表 | `prompts:popular:page:{n}` | 5 分钟 | 热门排序变化不频繁 |
| 分类内容列表 | `prompts:category:{cat}:sort:{sort}:page:{n}` | 2 分钟 | 按需缓存 |
| 单条详情 | `prompt:{id}` | 10 分钟 | 详情变化少 |
| 邮件验证码 | `verify:{email}` | 10 分钟 | 验证码过期时间 |
| 发送频率限流 | `send_limit:{email}` | 60 秒 | 防止验证码接口被刷（1 分钟 1 次） |
| 浏览量计数 | `view_count:{id}` | — | 批量写入，每 5 分钟刷入 DB |
| 热门搜索词排行 | `search:trending` | 1 小时 | 定时任务聚合 search_logs 后写入，前端搜索弹窗直接读缓存 |

---

## 七、项目目录结构

```
aigc-prompt-backend/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── config.py               # 环境变量配置（.env）
│   ├── database.py             # 数据库连接（async SQLAlchemy）
│   ├── redis.py                # Redis 连接
│   ├── models/                 # SQLAlchemy ORM 模型
│   │   ├── prompt.py
│   │   ├── user.py
│   │   └── interaction.py      # likes / collects
│   ├── schemas/                # Pydantic 请求/响应 Schema
│   │   ├── prompt.py
│   │   ├── user.py
│   │   └── auth.py
│   ├── routers/                # FastAPI 路由
│   │   ├── prompts.py
│   │   ├── auth.py
│   │   └── users.py
│   ├── services/               # 业务逻辑层
│   │   ├── prompt_service.py
│   │   ├── auth_service.py
│   │   └── email_service.py
│   └── utils/
│       ├── jwt.py              # JWT 生成/验证
│       ├── google_auth.py      # Google id_token 验证
│       └── oss.py              # 阿里云 OSS 工具
├── alembic/                    # 数据库迁移
│   └── versions/
├── tests/
├── .env                        # 环境变量（不提交 Git）
├── .env.example                # 环境变量示例
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

---

## 八、环境变量清单（.env）

```env
# 数据库
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/aigc_prompt

# Redis
REDIS_URL=redis://localhost:6379/0

# JWT
JWT_SECRET_KEY=your_secret_key_here
JWT_ALGORITHM=HS256

# Google OAuth
GOOGLE_CLIENT_ID=your_google_client_id

# 邮件服务（Resend）
RESEND_API_KEY=your_resend_api_key
EMAIL_FROM=noreply@aigc-prompt.com

# 阿里云 OSS
OSS_ACCESS_KEY_ID=
OSS_ACCESS_KEY_SECRET=
OSS_BUCKET_NAME=
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
OSS_CDN_DOMAIN=https://cdn.aigc-prompt.com
```

---

## 九、部署方案（MVP 阶段）

```yaml
# docker-compose.yml 核心结构
services:
  api:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres, redis]

  postgres:
    image: postgres:16
    volumes: ["pg_data:/var/lib/postgresql/data"]

  redis:
    image: redis:7-alpine
    volumes: ["redis_data:/data"]

  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    # 反向代理到 api:8000
    # 同时 serve 前端静态文件
```

**部署步骤**：
1. 阿里云 ECS 轻量型（2核4G 即可跑起 MVP）
2. Docker Compose 一键启动
3. Nginx 反向代理 + SSL（Let's Encrypt 免费证书）
4. 域名解析到 ECS IP
5. ICP 备案（预计 20 个工作日，提前申请）

---

## 十、MVP 阶段暂缓，但需预留扩展位

| 能力 | 预留方式 |
|---|---|
| 微信扫码登录 | users 表预留 `wechat_openid` 字段，auth 路由预留 `/api/auth/wechat` 占位 |
| 用户上传图片 | OSS 预签名上传接口设计好，但前端暂不开放入口 |
| 视频类提示词 | prompts 表预留 `content_type` 字段（image/video），MVP 全为 image |
| 全文搜索 | 先用 PostgreSQL GIN 全文索引，量大时可迁移到 Elasticsearch |

---

*本文档为 MVP 后端技术方案，前端技术选型另行出文档。*
