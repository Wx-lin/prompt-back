# 开发进度文档（PROGRESS.md）

> 记录规则：**每完成一个功能模块，立刻在本文件追加一条记录，不要等到最后统一补**。
> 格式：`## [日期] 模块名`，内容包含：做了什么、涉及哪些文件、遇到的问题/取舍、验证方式。
> 这个习惯延续自项目的"过程留痕"工作规范——出问题时能快速定位是哪一步埋的坑，换人接手时能看懂历史决策。

---

## [2026-10-09] AI 内容审核服务上线 + 存量数据/草稿批量重审

### 做了什么

此前批量审核通过的35条内容上线后发现三类质量问题：封面图不是AI生成效果图（游戏截图/真人实拍/教程截图被当成效果图）、文案是原帖直接搬运（带话题标签和互动语气）、分类未经判断。接入 AI 预审服务解决这三个问题，并用它重新处理了存量数据和新草稿。

1. **新增 `app/services/content_review_service.py`**：单次结构化调用阿里云百炼 `qwen3.7-plus`（OpenAI 兼容接口），一次请求同时完成三件事：逐张图片判断是否为 AI 生成效果图（`is_credible` + 理由）、清洗文案提取有效提示词、建议分类（8个创作意图分类之一）。显式传 `extra_body={"enable_thinking": False}` 关闭思考模式，省 token。方案细节、流程图、提示词原文见 `docs/aigc-prompt-内容审核方案.md`（新建）。
2. **`cos_service.py` 新增 `migrate_remote_image_to_cos`**：下载第三方 CDN 直链图片（小红书 `xhscdn.com`）后转存到自己的 COS，解决 AI 服务无法直接访问带防盗链的第三方图片的问题。
3. **`crawl_service.approve_draft` 接入 AI 预审**：在 COS 上传前调用审核服务，用建议值补全未显式传入的 `prompt_text`/`category`；全部图片不可信时只打日志提醒，不自动拒绝（审核结果是建议，不是强制门禁）。
4. **一次性批量处理脚本**（跑完即废弃，未纳入常规流程）：
   - `scripts/review_offline_prompts.py` + `scripts/apply_offline_review.py`：对此前下线的35条存量数据重新跑审核，35条中30条重新上线（删除不可信图片、覆盖标题/文案/分类），5条全部图片不可信保持下线。
   - `scripts/review_pending_drafts.py`：对26条待审核草稿批量跑"转存COS→AI审核→创建正式内容"全流程，19条成功发布，5条全部图片不可信保持pending，2条本身没有配图保持pending。

### 涉及文件

- `app/services/content_review_service.py`（新增）
- `app/services/cos_service.py`（新增 `migrate_remote_image_to_cos` 及 `_upload_bytes_sync`）
- `app/services/crawl_service.py`（`approve_draft` 接入 AI 预审）
- `app/config.py`（新增 `dashscope_api_key` 配置项）
- `.env`（新增 `DASHSCOPE_API_KEY`）
- `docs/aigc-prompt-内容审核方案.md`（新增，含 Mermaid 流程图）
- `scripts/review_offline_prompts.py`、`scripts/apply_offline_review.py`、`scripts/review_pending_drafts.py`（新增，一次性批量处理脚本）

### 遇到的问题 / 已知局限

1. **审核时序欠账**：`qwen3.7-plus` 只接受公网 URL，不接受本地文件/base64。但 `approve_draft` 的草稿图片这时还是本地路径，所以只能对"已经是公网URL"的图片审图，本地路径图片跳过审图（文案清洗/分类建议仍执行）。正确做法应该是先传COS再审核，当前为了快速验证效果没有重排 `approve_draft` 的步骤顺序，留作后续优化项。批量脚本（`review_pending_drafts.py`）因为不受线上接口历史实现约束，采用了更合理的"先传COS再审核"顺序。
2. **第三方CDN防盗链**：只带 `User-Agent` 下载小红书图片仍返回403，补上 `Referer: https://www.xiaohongshu.com/` 才成功——这类防盗链校验的是请求来源页面，不是签名级别防护。
3. **没有人工复核机制**：批量脚本里 AI 判断一次就直接采信，没有二次确认环节；分类非法值有兜底（降级为 `illustration`），但 `is_credible` 判断本身没有任何校验，完全依赖模型单次判断准确率。

### 真实验证记录

| 验证项 | 结果 |
|---|---|
| 单张图片+文案走完整审核链路（COS图） | ✅ 正确识别"真人实拍+卡通头像合成"非纯AI生成 |
| 小红书CDN直链图片直接送审 | ❌ 403，确认是防盗链拦截，非代码bug |
| 加 Referer 后迁移小红书图片到COS | ✅ 成功 |
| 存量35条数据批量重审 | ✅ 149张图迁移COS全部成功，30条发布/5条保持下线 |
| 26条待审核草稿批量处理 | ✅ 19条发布/5条不可信保持pending/2条无图保持pending |

### 下一步建议

1. 重排 `approve_draft` 的步骤顺序为"先COS上传、再AI审核"，消除当前的时序欠账
2. 考虑加"AI审核结果先存草稿字段、人工二次确认后才生效"的中间态，而不是单次判断直接采信
3. 保持 pending 的7条草稿（5条内容本身是资讯/测评类非效果图素材、2条没有配图）需要去后台手动忽略或补图
4. 调用成本未做监控，长期跑量后应加日志统计 token 消耗

---

## [2026-10-09] 分类展示修正：卡片展示分类而非模型标签

### 做了什么

用户反馈画廊卡片下方展示的"models"字段（如"即梦"、"Midjourney"）对浏览发现场景没有筛选价值，分类信息更有用。把 `prompt-card.tsx` 两处 `models.join(" · ")` 替换为分类中文标签，复用 `site-header.tsx` 已有的 `categories` i18n namespace 保持风格统一。`category` 字段此前已经在 `GalleryCard` 类型里，只是没有展示，改动很小。

### 涉及文件

- `aigc-prompt-frontend/src/features/gallery/components/prompt-card.tsx`（新增 `categoryLabel` 函数，替换卡片悬浮层和卡片下方两处展示）

### 验证方式

浏览器自动化打开首页，确认卡片实际渲染出"工作流 & 技巧"等中文分类标签，而非模型名或未翻译的 i18n key。

---

## [2026-10-08] 后台管理面板（前端）

### 做了什么

给上一条记录的后台管理API（`/api/admin/*`）配了一个能直接操作的界面，不用每次都手写curl或者在Swagger UI里一个个试。

1. 新建 `admin-panel/` 目录，纯 HTML + 原生 JS（`index.html` + `app.js`），零构建零依赖，双击打开或丢静态服务器就能跑。技术选型理由见 `admin-panel/README.md` 第一节——这是内部运营工具，没必要为了"统一技术栈"上框架。
2. 功能覆盖：登录（密钥存 sessionStorage）、内容列表（筛选发布状态）、新建/编辑/发布/下架/删除内容、举报列表（筛选处理状态）、处理举报（可联动下架）。
3. 后端 `.env` 的 `CORS_ORIGINS` 新增了管理面板的本地地址 `http://localhost:5500`，不加这个浏览器会直接拦截所有请求。

### 涉及文件

- `admin-panel/index.html`（新增）
- `admin-panel/app.js`（新增）
- `admin-panel/README.md`（新增，使用说明）
- `.env` / `.env.example`（`CORS_ORIGINS` 补充管理面板地址）

### 遇到的问题

1. **CORS 拦截**：管理面板起在 `http://localhost:5500`，后端最初的 `CORS_ORIGINS` 只配了产品主站的 `http://localhost:3000`，第一次测试登录直接被浏览器拦截，报 `No 'Access-Control-Allow-Origin' header`。加上 `5500` 后解决。**接手者注意**：如果管理面板换了端口/域名跑，记得同步更新 `CORS_ORIGINS`，否则不会有明显的后端报错，浏览器端会直接拦截请求、什么都看不到。
2. **CORS 预检缓存的坑**：第一次修复配置后用同一个浏览器标签页重试，仍然报同样的错——不是修复没生效，是浏览器把失败的 CORS 预检请求（OPTIONS）结果缓存了（后端返回的 `Access-Control-Max-Age: 600` 意味着10分钟内不会重新发预检）。**用curl单独验证了服务端配置其实已经生效**（`curl -X OPTIONS ... -H "Origin: ..."` 能看到正确的 `access-control-allow-origin` 响应头），换一个全新标签页后就正常了。遇到"改了CORS配置但浏览器还是报同样的错"，先怀疑是预检缓存，不要怀疑配置没生效。

### 真实验证记录（浏览器自动化走完整流程，非凭感觉写代码）

| 验证项 | 结果 |
|---|---|
| 打开面板显示登录表单 | ✅ |
| 输错密钥/CORS配置错误时的失败态 | ✅ 页面正确显示错误信息（修复CORS后变为可登录） |
| 输入正确密钥登录 | ✅ 真实调用了一次 `/api/admin/prompts` 验证密钥，成功后进入主界面 |
| 新建内容（填写标题/提示词/分类/模型/作者，不发布） | ✅ 提交后弹窗关闭，列表正确显示新内容，状态为"草稿" |
| 点击"发布" | ✅ 状态变为"已发布" |
| 点击"编辑"，弹窗正确预填所有已有字段（包括分类下拉框选中正确项、发布状态checkbox） | ✅ |
| 切换到"举报管理"tab | ✅ 正确显示举报列表，`内容标题`列正确关联显示 |
| 点击"处理"，填写备注并勾选"同时下架"，提交 | ✅ 状态变"已处理"，处理备注正确显示 |
| 验证联动下架真实生效（用curl查公开接口和后台接口） | ✅ 公开接口 `total: 0`，后台接口 `is_published: False` |
| 清理测试数据（删除接口） | ✅ 204 |

### 验证方式

```bash
# 启动后端
cd ~/Desktop/aigc-prompt-backend && uv run uvicorn app.main:app --reload --port 8000

# 启动管理面板
cd ~/Desktop/aigc-prompt-backend/admin-panel && python3 -m http.server 5500

# 浏览器打开 http://localhost:5500，用 .env 里的 ADMIN_SECRET_KEY 登录
```

### 下一步建议

1. 没有做图片上传功能（见 `admin-panel/README.md` 第四节），效果图需要先传到OSS/图床拿到URL再手动填，等OSS配置好、内容量上来后可以考虑加"选文件自动上传"
2. `confirm()` 原生对话框（删除内容时的二次确认）在浏览器自动化测试里不太好处理，这次测试跳过了"删除按钮点击确认"这一步UI交互，改用curl直接验证了删除接口本身的正确性（上一条记录已经验证过），如果以后要再测这块UI，注意浏览器自动化对原生 `confirm`/`alert` 的处理方式

---

## [2026-10-08] 后台管理接口（内容管理 + 举报处理）

### 做了什么

PRD 3.5"内容管理（后台运营，非用户可见）"原本写的是"MVP阶段无后台，直接数据库/表格手动录入"，但结合爬虫采集方案（见 `docs/aigc-prompt-爬虫方案.md` 的人工审核环节）重新评估后，判断手写SQL录入内容的操作体验太差、容易出错，补齐了一套最小化的后台管理API：

1. **鉴权方式**：不复用面向普通用户的 User/JWT 体系（语义不符，普通用户登录和"运营方管理后台"是两件不同的事），改用一个固定的管理员密钥，通过请求头 `X-Admin-Key` 校验（`app/utils/admin_auth.py`），足够简单、MVP阶段够用。
2. **内容管理接口**（`app/routers/admin.py`，`/api/admin/prompts/*`）：列表（支持按发布状态筛选）、详情、创建（默认草稿态）、编辑（PATCH部分更新）、发布、下架、删除。
3. **举报管理接口**（`/api/admin/reports/*`）：列表（支持按处理状态筛选，附带被举报内容标题）、处理（标记已处理+记录备注，可选联动下架对应内容），对应 PRD 3.5"下架申诉处理"（P0）—— 这个之前只有"用户提交举报"的写入接口，没有任何管理端能看到和处理这些举报，是一个真实的功能缺口，这次一起补上了。
4. 新增 `app/services/admin_service.py` 承载具体的数据库操作逻辑，路由层只做参数解析和权限校验。
5. 新增 `app/schemas/admin.py` 定义后台专用的请求/响应结构（和对外的 `app/schemas/prompt.py` 分开，因为后台需要暴露 `is_published` 等管理字段）。
6. `.env` / `.env.example` 新增 `ADMIN_SECRET_KEY` 配置项。

### 涉及文件

- `app/utils/admin_auth.py`（新增）
- `app/schemas/admin.py`（新增）
- `app/services/admin_service.py`（新增）
- `app/routers/admin.py`（新增）
- `app/main.py`（挂载 admin 路由）
- `app/config.py`（新增 `admin_secret_key` 配置项）

### 遇到的问题 / 真实发现的 bug

1. **鉴权返回状态码不对**：`verify_admin_key` 最初用 `Header(...)` 声明必填请求头，没带 `X-Admin-Key` 时 FastAPI 直接返回 **422**（"缺少必填参数"），而不是语义正确的 **401**（鉴权失败）。422 还会在响应里暴露"这个接口需要哪个请求头"的内部细节，不够安全。真实 curl 测试时发现这个问题，改成 `Header(None)` 手动判断 `None` 的情况，统一返回 401。

2. **真实发现的 bug：`update_prompt` / `create_prompt` 的 `db.refresh()` 范围不够**，调用编辑接口 (`PATCH /api/admin/prompts/{id}`) 时直接 500：
   ```
   pydantic_core._pydantic_core.ValidationError: 1 validation error for AdminPromptOut
   updated_at
     Error extracting attribute: MissingGreenlet: greenlet_spawn has not been called...
   ```
   原因：代码里只写了 `await db.refresh(prompt, attribute_names=["images"])`，只刷新了 `images` 这一个字段。但 `updated_at` 字段有数据库端 `onupdate` 触发器，提交后数据库里的值已经变了，本地 ORM 对象上还是旧值；Pydantic 序列化时尝试访问这个"脏"字段触发了额外的异步IO，但当时已经不在正确的 async 上下文里，抛出 `MissingGreenlet`。
   修复：改成先 `await db.refresh(prompt)`（整体刷新所有字段）再 `await db.refresh(prompt, attribute_names=["images"])`（专门刷新关联对象，因为整体refresh不会自动带上关联的`images`）。`create_prompt` 里有一样的问题，一起修了。
   **这是继上次 `.overlap()` 之后，第二个只有真实请求才能暴露的bug**，再次印证了"真实测试"这件事不能跳过。

### 真实验证记录（本机 PostgreSQL + Redis，服务跑在 localhost:8000）

| 验证项 | 结果 |
|---|---|
| 不带 `X-Admin-Key` 访问后台接口 | ✅ 正确返回 401（修复前是语义不对的422） |
| 带错误密钥访问 | ✅ 正确返回 401 |
| 带正确密钥创建一条草稿内容 | ✅ 返回 `is_published: false` |
| 草稿态内容在公开接口 `/api/prompts` 不可见 | ✅ 验证通过（total从0开始） |
| 后台按 `is_published=false` 筛选草稿列表 | ✅ 能查到刚创建的草稿 |
| 调用 `/publish` 发布 | ✅ `is_published` 变为 true |
| 发布后公开接口立即可见 | ✅ 验证通过 |
| 调用 `PATCH` 编辑标题 | ❌ 首次500（见上方bug记录）→ 修复后 ✅ 正常返回，`updated_at` 字段正确更新 |
| 走公开举报接口提交一条举报 | ✅ 204 |
| 后台举报列表能查到，且 `prompt_title` 正确关联显示 | ✅ 通过 |
| 调用 `/handle` 处理举报并勾选"联动下架" | ✅ 举报状态变 resolved，同时对应内容立即从公开接口消失 |
| 删除内容（`DELETE`） | ✅ 204，再查详情返回404 |
| 举报记录随内容级联删除 | ✅ 验证通过（`ON DELETE CASCADE` 外键生效） |

### 验证方式

```bash
cd ~/Desktop/aigc-prompt-backend
uv run ruff check .      # 通过
uv run pytest -v         # 4/4 通过
# 真实接口测试见上方"真实验证记录"表格，调用时需要在请求头带 X-Admin-Key
curl http://localhost:8000/api/admin/prompts -H "X-Admin-Key: <ADMIN_SECRET_KEY>"
```

### 下一步建议

1. 这几个后台接口目前只能通过 Swagger UI (`/docs`) 或 curl 调用，没有做任何管理界面；如果后续觉得每次都要带密钥手动调用不方便，可以考虑做一个极简的内部管理页面（见此前"选项B"讨论），但MVP阶段不是必须的
2. `docs/aigc-prompt-后端技术选型.md` 仍未同步这部分新增的后台管理API设计说明，和之前遗留的 `ContentReport`/`SearchLog` 两张表一起，找个时间统一补一次技术方案文档
3. `docs/openapi.json` 已重新导出，当前共23个接口（新增了6个admin接口），接口有变动记得重新导出覆盖

---

## [2026-10-08] 项目初始化 + 后端骨架搭建 + 真实环境端到端验证

### 做了什么

1. 用 `uv init` 创建项目骨架，Python 3.13。
2. 按《后端技术选型.md》第七节的目录结构，建好 `app/models` `app/schemas` `app/routers` `app/services` `app/utils` 五层分层。
3. 实现了 PRD 中 P0 功能对应的全部后端代码：
   - **数据库模型**（`app/models/`）：`Prompt`、`PromptImage`、`User`、`UserLike`、`UserCollect`、`SearchLog`、`ContentReport` 共 7 张表（原技术方案文档只设计了 5 张，开发时补齐了 `SearchLog`——对应搜索弹窗热门词功能，以及 `ContentReport`——对应 PRD 中要求但技术方案最初遗漏的举报/下架申诉功能）
   - **接口**（`app/routers/`）：
     - `prompts.py`：首页列表、搜索、详情、相似推荐、浏览/点赞/收藏、举报
     - `auth.py`：邮箱验证码登录、Google 登录、获取当前用户
     - `users.py`：我的收藏列表
     - `categories.py`：分类列表（含每个分类的内容计数）
     - `search.py`：热门搜索词、记录搜索日志
   - **业务逻辑**（`app/services/`）：`prompt_service`、`auth_service`、`email_service`、`search_service`、`view_count_service`
   - **工具**（`app/utils/`）：JWT 生成解析、Google id_token 验证、OSS 工具、认证依赖（`get_current_user` / `get_optional_current_user`）
   - **定时任务**（`app/scheduler.py`）：每 5 分钟批量回写浏览量到数据库、每小时聚合热门搜索词排行
4. 配置好 Alembic 异步迁移环境（`alembic/env.py` 改成异步引擎 + 从 `app.config` 读取数据库地址，而非默认模板写死在 `alembic.ini`）。
5. 配置 ruff 做代码风格检查，所有代码跑过 `ruff check` 全部通过（已排除 `alembic/versions/` 自动生成目录，不手动改动生成代码）。
6. 写了 4 个单元测试（JWT 生成解析、分类标签映射），全部通过。
7. 准备好 `Dockerfile`、`docker-compose.yml`、`.env.example`，支持一键启动 Postgres + Redis + API。
8. 把产品决策文档（调研、PRD、技术选型）同步到项目 `docs/` 目录，和代码放在一起留痕。
9. **用 Homebrew 本地装了真实的 PostgreSQL 16 和 Redis，做了一轮完整的真实环境端到端验证**（见下方"真实验证记录"），不是只做语法检查。

### 涉及文件

几乎整个 `app/` 目录为新建，详见上方清单。关键文件：
- `app/main.py`（应用入口）
- `app/models/__init__.py`（**新增模型后必须在这里补 import**，否则 Alembic 识别不到）
- `alembic/env.py`（异步迁移配置）
- `alembic/versions/ff6f26c6fa2d_init_tables.py`（**真实生成并真实执行过**的首个迁移脚本，已验证能在真实 PostgreSQL 建出全部 7 张表）

### 遇到的问题 / 取舍

1. **技术方案文档遗漏的功能，开发时顺手补齐**：原《后端技术选型.md》只设计了 5 张表，缺少"举报/下架申诉"（PRD 3.2 明确要求的 P0 功能）和"热门搜索词"（后来讨论新增的功能）对应的表。开发时新增了 `ContentReport` 和 `SearchLog` 两张表，技术方案文档本身**暂未同步更新**，这是一个遗留的文档滞后问题，下次更新技术方案文档时需要补上这两张表的说明。

2. **代码风格修正**：开发过程中写出过一些不规范的写法（函数内部临时 import、用 `__import__()` 动态引用而非正常 import），已在同一轮次内发现并修正为标准写法。

3. **真实环境验证中发现并修复了一个真实 bug**：`prompt_service.list_prompts` 里按模型标签筛选的代码写成了 `Prompt.models.overlap(models)`，这个 `.overlap()` 方法在 SQLAlchemy 2.0 的 PostgreSQL ARRAY 字段上**根本不存在**，之前只做了语法检查和模块导入检查，没发现这个问题——因为它不是语法错误，是运行时才会暴露的 `AttributeError`。
   真实请求 `GET /api/prompts?models=即梦` 时触发了 `500 Internal Server Error`，排查后改成了 PostgreSQL 原生的 `.op("&&")` 数组重叠操作符（`Prompt.models.op("&&")(array(models))`），重新验证后筛选逻辑（包含匹配、不匹配两种情况）均返回正确结果。
   **这证明了"代码能导入、lint 能通过"不等于"接口能正常工作"，之前几轮给出的"已验证"结论是不完整的，这次才是真正跑通了请求链路。**

4. **邮件/OSS/Google 登录三个外部服务未做真实集成测试**：这三个功能依赖外部服务的真实密钥（Resend API Key、阿里云 OSS 密钥、Google OAuth Client ID），本次验证中 `send-code` 接口在没有真实 Resend Key 的情况下按预期返回了 500（`API key is invalid`），**这是预期失败，不是 bug**——它同时证明了验证码生成和写入 Redis 这部分逻辑是对的（真实在 Redis 里查到了生成的 6 位验证码），只是真实发信这一步因为没有可用密钥而失败。`verify-code`、Google 登录两个接口的核心逻辑（查找/创建用户、签发 JWT）已经用"手动从 Redis 读出验证码再调用验证接口"的方式绕过了真实发信依赖，完整验证通过；Google 登录因为没有真实 Google OAuth Client ID，**没有验证**，需要接手者申请密钥后自行验证。

### 真实验证记录（本机临时装 PostgreSQL 16 + Redis，验证完成后已清理测试数据库）

验证环境：`brew install postgresql@16`、`redis-server`（均为本机 Homebrew 安装，验证完成后数据库已删除，服务保留运行供后续继续使用）。

真实跑通的流程（均为真实 HTTP 请求 + 真实数据库读写，非 mock）：

| 验证项 | 结果 |
|---|---|
| `alembic revision --autogenerate` 识别全部 7 张表并生成迁移脚本 | ✅ 通过 |
| `alembic upgrade head` 在真实 PostgreSQL 建表成功 | ✅ 通过 |
| 插入一条含 PostgreSQL ARRAY 字段的真实测试数据 | ✅ 通过 |
| `GET /health` 健康检查 | ✅ 通过 |
| `GET /api/categories` 返回 7 个分类，且 count 随数据变化正确联动 | ✅ 通过 |
| `GET /api/prompts` 首页列表（空数据 + 有数据两种情况） | ✅ 通过 |
| `GET /api/prompts?category=xxx` 分类筛选 | ✅ 通过 |
| `GET /api/prompts?models=xxx` 模型筛选 | ❌ 首次测试报 500 → 定位到 `.overlap()` 用法错误 → 修复后 ✅ 通过（含匹配与不匹配两种情况） |
| `GET /api/prompts/search?q=xxx` 关键词搜索 | ✅ 通过 |
| `GET /api/prompts/:id` 详情接口（含未登录 is_liked/is_collected 均为 false） | ✅ 通过 |
| `GET /api/prompts/:id/similar` 相似推荐 | ✅ 通过 |
| `POST /api/prompts/:id/view` 匿名浏览量记录 | ✅ 通过（204） |
| `POST /api/prompts/:id/report` 举报（无需登录），真实落库验证 | ✅ 通过，`psql` 查询确认 `content_reports` 表真实写入一条 `status=pending` 记录 |
| 未登录调用 `POST /api/prompts/:id/like` | ✅ 正确返回 401 |
| `POST /api/auth/send-code` | 预期内失败（无真实 Resend Key），但验证码生成 + 写入 Redis 逻辑验证通过 |
| `POST /api/auth/verify-code`（用 Redis 里的真实验证码） | ✅ 通过，真实创建了 User 记录并返回有效 JWT |
| 用真实 JWT 调 `GET /api/auth/me` | ✅ 通过，返回正确的用户信息 |
| 用真实 JWT 调 `POST .../like`、`POST .../collect` | ✅ 通过，`like_count`/`collect_count` 冗余计数字段真实 +1 |
| 重复点赞幂等性验证（唯一约束生效，不会重复 +1） | ✅ 通过 |
| `DELETE .../like` 取消点赞，计数真实 -1 | ✅ 通过 |
| `GET /api/users/me/collects` 我的收藏列表 | ✅ 通过 |

### 验证方式

```bash
cd ~/Desktop/aigc-prompt-backend

# 代码风格检查（已验证通过）
uv run ruff check .

# 单元测试（已验证通过，4/4）
uv run pytest -v

# 模块导入检查（已验证通过）
uv run python -c "import app.main; print(len(app.main.app.routes))"

# 真实环境端到端验证（本次已完整执行一遍，见上方"真实验证记录"表格）：
brew services start postgresql@16   # 或用 docker compose up -d postgres redis
brew services start redis
createdb aigc_prompt_dev            # 创建数据库后记得在 .env 配置对应的 DATABASE_URL
uv run alembic upgrade head         # 迁移脚本已生成在 alembic/versions/，直接升级即可
uv run uvicorn app.main:app --reload --port 8000
# 然后访问 http://localhost:8000/docs 测试接口，或参考上方真实验证记录里的 curl 示例
```

### 下一步建议

1. ~~装好 Docker，跑通数据库迁移，手动验证核心接口~~ **已完成**（本次用 Homebrew 本地 PostgreSQL + Redis 完成，不依赖 Docker 也能跑通）
2. 补充 `app/services/prompt_service.py` 等核心业务逻辑的**自动化**单元测试（本次是手动 curl 验证，验证完成后环境已清理，没有留下可重复运行的集成测试代码，建议后续补一套基于真实数据库 fixture 的 pytest 集成测试，避免每次改动都要手动重新搭环境验证）
3. 更新 `docs/aigc-prompt-后端技术选型.md`，补充 `ContentReport`、`SearchLog` 两张表的设计说明（当前只在代码注释里写了，文档没同步），同时补充这次发现的 `.overlap()` → `.op("&&")` 修正说明
4. ~~申请 Google OAuth Client ID、Resend API Key、阿里云 OSS 密钥~~ **部分完成**，见下方"外部服务集成真实验证记录"
5. 开始录入第一批内容（人像/头像分类，50-100条，见 PRD 待确认事项）

---

## [2026-10-08] 外部服务集成真实验证记录（Google 登录 / Resend 邮件 / 阿里云 OSS）

### Google OAuth 登录

1. 用户在 [Google Cloud Console](https://console.cloud.google.com/) 新建项目 `aigc-prompt`，配置 OAuth 权限请求页面（External 用户类型），创建 OAuth 2.0 客户端 ID（应用类型选"网页应用"，授权 JavaScript 来源填 `http://localhost:3000`）。
2. 拿到真实 Client ID：`466465886573-k0ngu0578j3krpot6ghfaudp255sturu.apps.googleusercontent.com`，已写入本机 `.env` 的 `GOOGLE_CLIENT_ID`。
3. 用真实 Client ID 搭了一个最小化的 Google 登录测试页（Google Identity Services 官方 JS SDK + 原生 fetch 调用后端 `/api/auth/google`），跑在 `http://localhost:3000`，和后端 `http://localhost:8000` 配合测试。
4. **踩坑记录**：刚创建完 OAuth 客户端立即测试，报错 `The given origin is not allowed for the given client ID`（403）。Google 官方提示过"设置可能需要5分钟到几小时才会生效"，**这是 Google 一侧的配置生效延迟，不是代码问题**。等待几分钟后重试，弹窗正常弹出，说明授权来源配置生效。
5. 后端 `verify_google_id_token`（`app/utils/google_auth.py`）用真实 id_token 校验的链路验证**进行中**（已确认 Client ID 配置生效，弹窗能正常弹出；真实登录拿到 id_token 并回传后端验证的最终结果待补充）。

### Resend 邮箱发信（**真实验证通过**）

1. 用户在 [resend.com](https://resend.com) 注册账号，创建 API Key，已写入 `.env` 的 `RESEND_API_KEY`（密钥不记录在文档中）。
2. `EMAIL_FROM` 配置为 Resend 默认测试域名 `onboarding@resend.dev`（**注意**：这个测试域名发信有限制，只能发给注册 Resend 账号时验证过的邮箱，不能发给任意用户邮箱；真实上线前需要去 Resend 后台验证自己的域名 `aigc-prompt.com`，加几条 DNS 记录，才能对任意用户邮箱发信）。
3. 真实调用链路验证：
   - `POST /api/auth/send-code`，邮箱填用户注册 Resend 时用的真实 Gmail 地址 → 返回 `204 No Content`
   - 用户真实在 Gmail 收件箱收到了标题为"你的 AIGC提示词 登录验证码"的邮件，内容包含6位验证码
   - 用邮件里的真实验证码调用 `POST /api/auth/verify-code` → 返回真实 JWT（`access_token`）
   - 用这个真实 JWT 调用 `GET /api/auth/me` → 返回正确的用户信息，确认数据库里真实创建了 User 记录
4. **这是本次所有外部服务集成测试中第一个完整走通"真实发信 + 真实收信 + 真实登录"全链路的**，之前的自测只验证到了验证码生成和写入 Redis，没有真实发出邮件。

### 阿里云 OSS（图片存储）

尚未开始申请和验证，后续需要去阿里云控制台创建 Bucket，获取 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_BUCKET_NAME` 等配置。

### 本机 `.env` 实际配置说明（和 `.env.example` 的差异）

本机 PostgreSQL 是用 Mac 系统账号 `wangxinlin` 直连（无密码），不是 `.env.example` 模板里默认的 `postgres/postgres`，正式开发用的数据库连接串是：
```
DATABASE_URL=postgresql+asyncpg://wangxinlin@localhost:5432/aigc_prompt_dev
```
`JWT_SECRET_KEY` 也已经从模板的占位值 `CHANGE_ME_IN_PRODUCTION` 换成了随机生成的真实密钥。**这个 `.env` 文件不提交到 git**（已在 `.gitignore` 排除），接手者需要根据自己的本机环境配置对应的值，不能直接照抄这里写的本机专属值。



## [2026-10-09] 腾讯云独立部署、数据迁移和每日自动审核任务

- 新建 `deploy/` 生产Compose、镜像、HTTPS网关、systemd采集/证书续期服务。
- 本机前端适配Next standalone，管理端支持 `/admin/`；部署至62.234.69.93独立端口8443，8082跳转。
- 迁移PostgreSQL数据及159个本地草稿图片文件，数据库计数一致；新数据库/Redis仅内网访问。
- 新增 `scripts/scheduled_crawl.py`、`auto_review_service.py`、跨进程采集锁，先COS后严格AI审核，只自动发布本轮新草稿，失败留pending；Chrome支持Linux、无头模式、任务结束回收与超时。
- 新增单元/隔离PostgreSQL集成测试，审核/锁/CLI覆盖率91%；公网主站、管理端鉴权、用户会话和收藏接口验证通过。
- 每日10:00/22:00定时器已启用，首次2026-10-10 10:00。
- **未完成真实采集验证**：小红书返回服务器IP风险300012，无法扫码登录，测试任务failed未发布。自动采集可用性仍依赖解决平台网络限制；不能将定时器已安装等同于采集成功。
- 完整操作说明、限制与回滚见 `docs/deployment/tencent-server.md`，不记录任何密钥。

- HTTPS证书自动续期服务已启用，模拟续期重试通过；最终后端27项测试全部通过。

## [2026-10-09] 小红书采集对照诊断与服务器浏览器调整

- 用户要求立即试采少量数据。3条试采仍被无头浏览器环境拦截，新增草稿/发布均0。
- 使用Xvfb中的有界面Chromium做独立对照，正常打开小红书，未出现IP-risk/300012，并检测到登录二维码。由此修正此前“仅服务器IP无法使用”的判断：浏览器运行环境是可调整的影响因素，尚不能把有界面访问成功等同于完整采集成功。
- 生产镜像增加私有Xvfb显示环境，`CHROME_HEADLESS=false`；不开放显示或CDP公网端口。旧未登录浏览器目录保留为服务器备份，使用新的专用profile等待用户扫码。
- 后端导入草稿增加请求数量上限，3条小批量入库上限测试先失败后通过，避免第三方默认20条整页返回导致小批量超额发布。
- 后续仍需：用户扫码→真实采集3条→AI审核→入库验证。

## [2026-10-09] 服务器扫码登录后首次真实采集、AI审核与发布成功

- 用户在服务器二维码完成小红书登录；重启独立worker后会话有效，实际采集落盘12条原始记录。
- 暴露并修复真实bug：MediaCrawler输出大JSON日志行超过asyncio默认64KiB，报`Separator is found, but chunk is longer than limit`。子进程StreamReader上限改4MiB、每行存储日志截断8000字符；先失败测试、再修复，并用真实20万字符子进程日志回归测试验证。
- 为避免重复抓取，使用本次12条落盘记录恢复导入3条新草稿（不处理历史pending），执行先COS→AI审核→正式内容事务发布。结果：imported3、published3、held0、skipped0。
- 新内容：实景转纸雕海报（5图）、AI绘画现状与技巧（16图）、实景延伸线稿旅行海报（9图）。保留来源作者与链接。
- 核验：数据库正式内容54→57、草稿61→64；公网上线内容49→52，新3条由API返回。Linux/Xvfb配置、登录状态和日志修复均已部署；每日10:00/22:00timer仍启用，下一次2026-10-10 10:00。
- 此前仅凭无头页面风险提示判断服务器IP不可用不充分；实际更换有界面浏览器及专用profile、用户扫码后已成功采集。后续仍可能需要平台要求的重新登录。


## 2026-10-13 · 用户投稿、人工审核与模型版本目录

- 新增私有上传资产、Prompt 提交人/审核字段及迁移 `e7a13c9b40d2`；登录用户上传/投稿/我的作品与独立后台通过/拒绝接口。所有权、重复绑定、过期、格式/字节/像素和限流校验；通过前图片 private ACL+短时签名，失败恢复、下架私有化及定时清理。公开详情和收藏列表过滤未发布。
- 主站新增发布表单与我的作品，后台新增投稿审核；主站 HttpOnly BFF 校验浏览器 Host/Origin 与体积，返回固定错误，不回传服务内情。保留中英文、深浅主题与移动布局；登录安全回跳。
- 模型别名保留具体版本且按家族归组；目录主站/后台共用，新写入及返回规范化，SQL 首页/搜索兼容存量别名、先过滤排序再分页。无需旧数据归一化迁移。
- 验证：Python 全量 133/133（含隔离 PostgreSQL）；迁移在独立临时数据库完整升级、回退前一版、再升级通过；主站 88 项单元/组件测试、lint/typecheck/build 通过；后台 build/lint 通过（6条既有警告）。浏览器隔离数据库投稿与后台审核流程已通过；COS、Redis 和会话为测试替身，非真实云端验证。上线尚未执行。
- 浏览器集成重跑：以 `AIGC_TEST_DATABASE_URL` 指向本地测试数据库，在后端运行 `uv run --group dev python -m uvicorn tests.submission_e2e_app:app --host 127.0.0.1 --port 8100`；主站 `API_BASE_URL=http://127.0.0.1:8100 AIGC_DEMO_MODE=false npm run dev -- --port 3110`；后台 `VITE_API_BASE=http://127.0.0.1:8100 npm run dev -- --host 127.0.0.1 --port 5176`；主站 `PLAYWRIGHT_BASE_URL=http://127.0.0.1:3110 AIGC_SUBMISSION_INTEGRATION=1 npm run test:e2e -- e2e/submissions-integration.spec.ts --workers=1`。fixture 仅绑定本机，关闭后自动删除随机 schema，不能部署到线上。

- 最终收尾：常规浏览器46/46，隔离数据库投稿/后台审核桌面和手机4/4另行通过；投稿核心6模块语句覆盖80.4%。修复了commit rollback后ORM失效影响ACL恢复、清理任务仅前100条与后台PATCH未加锁3个并发边界；回归包含强制commit失败和101资产分页，最后复核无剩余阻塞。主站演示服务恢复到3100；测试服务/schema已停止并清理。

- 提交前依赖审计：主站和管理后台 npm audit 无漏洞；pip-audit 仍报告既有 python-jose 3.5.0（CVE-2026-85394）与 ecdsa 0.19.2（PYSEC-2026-1325），公告暂无修复版本。安全审查核对本地有效 HS256、显式算法 allowlist 和对称密钥路径，不满足 DER 公钥混淆或 ECDSA 签名侧信道利用条件，不阻塞本次提交，但不得宣称 Python 依赖审计全通过。生产实际配置需部署时核对。公告：https://api.osv.dev/v1/vulns/CVE-2026-85394 和 https://api.osv.dev/v1/vulns/PYSEC-2026-1325。

## 2026-10-13 · 部署脚本检查与依赖清单修复

- 既有 `scripts/deploy_backend.py --dry-run` 通过（130 passed, 3 skipped），脚本专用2项测试和远端shell语法检查通过，公开8443 `/health` 返回ok。旧SSH socket不可复用，当前免密SSH认证失败，未执行远端写入，未核验当前revision/磁盘。
- 原脚本只部署后端、不执行迁移、不更新主站/管理后台，因此不能一键完成本次发布；迁移不在head时会保护性停止。本次Pillow新增导致完整依赖构建，需要脚本规定的至少5GiB空间。
- 检查发现Docker依赖来自 `deploy/requirements.txt` 而非直接同步uv.lock；此前新增Pillow漏同步此导出文件。已使用frozen uv export重新导出并补齐Pillow，保留其他锁定版本。

## 2026-10-13 · 部署脚本显式迁移选项

- 用户实际运行停在 `Database migration required`，本地校验、SSH、上传和构建已成功，API未切换。补充 `--migrate` 显式许可，在已验证数据库备份后使用新镜像迁移，检查current=head再切换；默认保留停止保护。
- 迁移异常保留旧API，不自动downgrade，迁移日志写入发布备份目录。此次e7迁移已验证为向后兼容新增字段/表，支持既有镜像回滚。
- 部署测试先复现RED，7项脚本/mock-shell测试GREEN；覆盖默认拒绝、有许可成功、有许可失败、已在head、命令转发及打包。Ruff和Bash语法检查通过；未连接服务器或执行线上迁移。

## 2026-10-14 · 12 分类接入与可审核迁移（本地完成，生产待授权）

- 后端统一12类目录及中英文/顺序；投稿、后台、爬虫共用输入校验。不确定的采集分类保持待审核，不默认归入风景静物。后台历史分类响应保留字符串，避免未迁移记录导致整页500。
- 分类计数、公开列表/搜索/详情/相似/收藏/模型目录/精选共用公开条件；三类旧URL通过持久化原ID集合继续组合关键词、模型、排序与分页，非法分类422。
- 后台API目录选择含零库存，三列表按新分类筛选，投稿批准支持更正分类；主站全部分类、双语、零库存和旧分类提示配套接入。
- 新结构迁移9c35e1a62f4仅建兼容集合和批次日志。category_migration工具提供离线preview、只读export、显式目标apply/rollback；仅confirmed=true行改分类，拒绝夹带发布状态/正文修改。记录锁、事务日志、内容/图片比对与重复执行/回滚保护已验证。通用updated_at受互动更新影响，已从内容guard移除，以正文、模型、来源、审核状态和图片等字段保护编辑。
- 当前快照120条正式内容（115公开）、82待审核草稿。图文复核查看264图，95正式/11草稿候选，正式实际分类变化40条；23已发布异常条目逐条处置提案已生成，建议暂下架保留，未获批准。所有202审核行仍false，离线预演0可应用；44条公开记录目前仍是旧key，这是实际预演口径，与23条没有最终分类建议不同。
- 验证：后端全量221 passed（真实本机PostgreSQL独立schema）；主站142单测passed；后台8单测passed；主站+后台真实API/数据库桌面与手机4 E2E passed；后端ruff/compileall/Alembic head通过；主站lint/typecheck/build通过；后台lint/build通过（6条既有lint警告）。迁移服务覆盖率92%；目录新增模块100%；主站分类导航分支93.75%。不宣称全仓库覆盖率达标。
- 隔离测试服务已停止，相关schema检查为0。未提交、推送、发布、生产迁移或下架。执行说明在docs/deployment/2026-10-14-category-upgrade.md，清单/快照位于工作区artifacts/category-review-2026-10-14（不进入公开站点或Git仓库）。生产执行前须重新导出、核对并发变化、备份，并确认逐条归类与下架方案。

- 发布前复核：生产数据与10:33审核快照完全一致；用户已明确授权106条归类与23条暂下架及上线。将未发布迁移ID规范为纯hex `9c35e1a62f4`，避免现有发布revision门禁拒绝，不改自动迁移白名单。独立审查发现并修复旧分类重新上架路径，先RED再GREEN，新增后全量224 tests通过。主站/后台npm audit为0；Python保持既有python-jose/ecdsa公告告警，JWT显式HS256/对称secret路径经审查不满足攻击前提，不宣称Python audit为0。
