# 图片转提示词后端

2026-10-22：共用后端首版；网站和小程序通过同一组接口使用，现阶段未部署。

## 2026-10-22 生产发布准备

网站首发使用现有主站 BFF，不依赖 `/mini-api` 的新网关规则；本次保留线上原小程序网关，新增小程序公开入口另行验收。已核对生产数据库父版本 `d91a72f4b308`，迁移 `e04b75ad6312` 仅创建三张解析表及索引/约束，不修改已有用户、作品或互动记录。发布脚本先验证自定义格式备份，再用新镜像迁移；代码回滚保留新表与历史，生产不执行 downgrade。

本次用 main 对应提交的完整 GitHub Actions `workflow_dispatch`，参数 `reviewed_migration=e04b75ad6312`。只接受 d91→e04 或已到 e04；普通 push 自动迁移白名单保持不变。发布提交可用 `[skip ci]` 避免无参数 push 先构建再被迁移门禁拒绝；随后显式 dispatch 仍执行原有 Ruff、pytest、后台 lint/build、镜像构建和部署检查，不跳过验证。

生产根 Compose 不是仓库模板的直接副本，首次安装只能有范围地新增 `prompt-analysis-worker`，复用现有 API 镜像与 runtime.env，覆盖入口 `python -m app.workers.prompt_analysis`、384MiB/1CPU、130秒优雅停止，使用 `image-prompt` profile；不能覆盖其他服务、端口、挂载或密钥。先备份根 Compose/runtime.env，并通过 `docker compose config` 比较现有服务语义不变。首次 API/迁移完成后再显式启动，验证 Worker 与 API 镜像 ID 一致，并跑真实图片任务。

后续发布脚本会在构建/迁移前优雅停止原来运行的解析 Worker，API 健康后恢复同版本 Worker；失败先停新 Worker、恢复旧 API/tag，再恢复原来运行状态。服务不存在、未创建或原已停用时不自动启用。停止失败会中止构建。该生命周期有12个实际 shell 分支回归，与迁移发布门禁合计84项通过。

新增配置由受保护的生产 runtime.env 提供：功能/Worker开关、显式 DashScope 协议、`qwen3.8-max`、独立 `PROMPT_VISION_API_KEY` 字段、4096输出上限、每用户5/全站100任务每日。首次发布将用户已经授权并完成实图验证的解析凭据显式写入新字段，不替换原图库审核密钥；应用仍没有审核密钥回退逻辑。保留原 Google、Resend、JWT、数据库与 COS 配置，不覆盖整份环境文件。凭据仅在进程内经加密SSH标准输入传递，不进入命令参数、代码、发布日志或聊天。

发布前安全检查将 Pillow 升至13.0.0、certifi升至2026.10.19。Pillow 的 XBM/GIF 识别阶段问题可在 MIME 校验前触发，故纳入本次修复。[Pillow公告](https://github.com/python-pillow/Pillow/security/advisories/GHSA-xgh8-jp6q-p5x8)、[certifi公告](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2)。锁文件及部署 requirements 仅这两包升级；依赖变化必须走原有≥5GiB完整重建门禁。新版独立测试环境全量686通过，无shim、无跳过、无真实模型/邮件调用。

只读依赖审计还报告 python-jose/ecdsa 两个无上游修复的唯一公告；不能称审计完全清零。已核对生产 HS256、非公钥形态且非默认密钥，JWT解码显式限制算法，不满足DER公钥混作HMAC的条件；项目未使用ECDSA私钥签名/生成/ECDH。[jose条件](https://github.com/mpdavis/python-jose/issues/414)、[ecdsa公告](https://github.com/tlsfuzzer/python-ecdsa/security/advisories/GHSA-wj6h-64fc-37mp)。本次不关闭验证或替换认证机制。

## 能力与边界

- 一张静态 JPEG/PNG/WebP → 中英完整提示词、主体/构图/光线/风格拆解 → 私有历史。
- 任务与历史是同一条记录，成功落库后即可查询；编辑、复制、切语言不调用模型。
- 同一 FastAPI 工程提供 API，独立 Worker 使用同一代码领取 PostgreSQL 任务。Redis 管频率，PostgreSQL 管额度，图片在私有 COS。
- 新旧小程序分别使用固定 AppID 配置；账号默认按 AppID/OpenID 独立，网站和小程序历史不自动合并。
- 第一版没有自动模型重试；有发送意图后结果不确定，记录 unknown，避免盲目再次计费。再次解析须由用户创建新任务。
- 不包含视频、生图、支付或账号绑定。两张首页示例还未策划，`GET /api/prompt-examples` 目前返回空 items，不伪造已解析数据。

## 本地启动

先安装依赖：

```bash
uv sync --locked --dev
```

在自己的 `.env` 中填写数据库、Redis、COS 和模型配置，不提交密钥。新增字段见根目录 `.env.example`。独立模型密钥 `PROMPT_VISION_API_KEY` 不会回退到图库审核密钥。模型必须支持图片输入和 JSON 对象响应；本版只有 v1 系统模板，选择模型后需要实图评测。

迁移新增三张表：`prompt_analysis_assets`、`prompt_analyses`、`prompt_analysis_attempts`。迁移链为 `d91a72f4b308 → e04b75ad6312`。**数据库迁移需要先审阅，只在指定目标库执行。没有改自动迁移白名单，也没有在生产执行。**

```bash
uv run alembic upgrade head
```

完成配置后分别启动：

```bash
PROMPT_ANALYSIS_ENABLED=true uv run uvicorn app.main:app --port 8000
```

```bash
PROMPT_ANALYSIS_WORKER_ENABLED=true uv run python -m app.workers.prompt_analysis
```

API 和 Worker 都需读取同一份数据库、存储及模型配置。功能开关默认 false；缺模型配置时新建任务返回明确的 503；缺配置的 Worker 继续做恢复/过期维护，不领取新任务。已有历史仍可查看、编辑和删除。Worker 有自己的 2 连接池；API 5 连接、overflow 0，SQL 参数日志关闭。

### 当前模型：Qwen 3.8-Max

用户已选择 `qwen3.8-max`，本地独立配置如下，密钥留在 gitignored `.env`：

```dotenv
PROMPT_VISION_PROVIDER=dashscope
PROMPT_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
PROMPT_VISION_MODEL=qwen3.8-max
PROMPT_VISION_MAX_TOKENS=4096
# PROMPT_VISION_API_KEY 在本机安全填写，不提交
```

`dashscope` 适配器固定发送 `enable_thinking=false` 和 `max_completion_tokens`，使用 Base64 JPEG 输入及 `json_object` 响应，再做本地严格字段校验。其他兼容供应商显式选 `openai-compatible`，继续使用通用 `max_tokens` 参数。思考设置随任务保存快照，发送前配置变化会阻止旧任务派发。依据：[模型说明](https://help.aliyun.com/zh/model-studio/qwen3-8-max)、[Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)、[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)。

图库原有 `qwen3.7-plus` 审核模块不受此次选择影响。解析独立使用 `PROMPT_VISION_API_KEY`，没有隐式回退到审核密钥。记录供应商提供的输入、输出、总 token 数与可用的图片/文本/缓存/思考细分；细分已经包含在总量中，不再次相加。响应模型标识由适配器安全返回，任务配置保留原始请求模型。

2026-10-22 首次真实请求被 `HTTP 400 / Arrearage` 拒绝；模型列表可正常查询且包含 `qwen3.8-max`。用户恢复账户后，商品图与水墨海报均获得真实结构化结果，响应模型均为 `qwen3.8-max`。该错误的官方解释为欠费、余额不足或充值状态尚未同步，遇到时应检查账户，不能当作模型不存在或盲目改请求参数。[官方错误说明](https://help.aliyun.com/zh/model-studio/error-code#overdue-payment)

| 实图 | 尺寸 | 模型请求耗时 | 输入 / 输出 tokens | 验证路径 |
| --- | --- | --- | --- | --- |
| 香水静物 | 262×300 | 18.82秒 | 761 / 910 | 正式适配器直接请求供应商 |
| 水墨山河海报 | 299×333 | 12.56秒 | 779 / 654 | 实际API → PostgreSQL/Redis → 私有COS → Worker → 供应商 → 历史查询 |

第二条使用真实应用路由和进程内 ASGI HTTP transport，没有替换业务处理、COS或视觉供应商；幂等重放返回同一任务、模型仅调用一次、usage和request ID实际落库。历史验证后删除测试记录并清理对应私有COS对象，仅在聊天工作区保留演示结果。实际外网网关/网站BFF/小程序真机仍未验证。

两张样例能提取主体、布局和风格，水墨图识别出“山河”；香水瓶底部小字存在猜测，虽标记不确定，仍被写入提示词。此为当前效果边界，未验证重新生图还原、完整20图质量集或实际账单，不能据两图宣称全面稳定。

Compose 预置独立 profile，供以后审阅部署时使用：

```bash
docker compose -f deploy/compose.yml --profile image-prompt up -d prompt-analysis-worker
```

它覆盖原爬虫/Xvfb 入口，并非第二套后端。实际生产目录、runtime.env 和网关发布仍沿用既有流程，本轮没有执行。

## 登录

旧小程序仍为 `POST /api/auth/wechat`；新小程序为 `POST /api/auth/wechat/image-to-prompt`。两者 body 均只有 `{"code":"wx.login返回的code"}`，响应仍采用现有 TokenResponse，客户端不得提供任意 AppID、OpenID 或 Secret。

服务端配置 `WECHAT_IMAGE_TO_PROMPT_APP_ID/SECRET`。新旧 Token 分别含已签名 cid，网站旧令牌/无 cid 令牌继续兼容。cid 仅记录来源，不赋予更高权限。新小程序不要求填写昵称、头像或手机号；其平台域名与隐私配置需单独完成并真机核验。

## 业务 API

除固定示例外，以下接口均要求 `Authorization: Bearer <token>`。网站应通过现有会话 BFF 转发，保留 HttpOnly Cookie、Origin 校验及 no-store；本轮不改网站代码。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/prompt-assets` | multipart 字段 file，201 返回 data.id、尺寸、短期预览 URL |
| GET | `/api/prompt-assets/{id}` | 刷新本人可用资产预览 |
| POST | `/api/prompt-analyses` | body asset_id，UUID Idempotency-Key；首次202，重放200 |
| GET | `/api/prompt-analyses` | limit默认20、最多50，cursor分页 |
| GET | `/api/prompt-analyses/{id}` | status、result、当前有效prompt、revision、预览和保留期限 |
| PATCH | `/api/prompt-analyses/{id}` | body revision + prompt.zh/en；成功后revision+1 |
| DELETE | `/api/prompt-analyses/{id}` | 204，立即隐藏并保留额度/幂等墓碑 |
| GET | `/api/prompt-examples` | 公开可缓存的人工策划示例，目前为空 |

新业务响应为 `success / data / error / request_id`，私有响应 `Cache-Control: private, no-store`，旧接口格式不变。列表 `data.items` + `data.pagination(limit,next_cursor,has_more)`；详情 `data.result` 保存原结果，`data.prompt` 是用户编辑后的有效提示词。状态 queued/running/succeeded/failed/unknown/canceled；只有成功状态 `saved_to_history=true`。

POST示例（占位值需替换）：

```http
POST /api/prompt-analyses
Authorization: Bearer <token>
Idempotency-Key: <uuid>
Content-Type: application/json

{"asset_id":"<upload-response-id>"}
```

网络重试必须复用原幂等键；换图或主动重新解析用新键。已删除/过期记录同键重放404，不能复活。同键换 asset 返回409 IDEMPOTENCY_CONFLICT。每用户已有在途任务时新键返回409 ANALYSIS_IN_PROGRESS；并发编辑同修订号只有一个成功，其余409 REVISION_CONFLICT。

## 文件与费用控制

- 上传最多5 MiB、20MP、静态三格式，真实解码匹配MIME；EXIF方向修正、去元数据、透明铺白。标准图长边≤1536px/≤2MiB，缩略图≤320px，不保留原始上传。
- 私有随机 object key，API不暴露key、不接受远程图片URL。约600秒签名只用于预览；数据库保存key，Worker私有读取后发送给配置的模型API。
- 模型结果严格校验；不完整/截断/无效JSON不作为成功，也不自动再调模型。成功调用保存安全的request ID和token usage；失败使用安全码，不返回上游正文。
- SDK的私有COS签名和URL日志按请求上下文过滤；私有图片、完整提示词、令牌、模型原始响应不进访问/错误/监控日志。业务库按私有历史保留策略存结果。
- 默认每用户5任务/日、全站100任务/日、每用户1在途、排队最多10；上传每用户30/小时、全站300/日。Redis故障拒绝新增上传/解析，但读取、编辑、删除和幂等重放仍可用。
- 任务日固定北京时间创建日，删除不重置额度。发送前失败释放额度，已发送明确失败计入，unknown也计入。任务数量限额不等于严格的供应商当日费用封顶；跨日执行、供应商账单和账户预算需单独核实。

## 恢复和清理

Worker 领取后写租约；发送前再次锁资产/任务，校验租约、归属、有效状态和配置快照，再持久化 dispatch_intent。无发送意图的丢失任务可恢复，有发送意图则unknown，不盲重发。模型结果与成功状态一起提交，落库重试不会再次调模型。

默认租约90秒、心跳15秒、模型调用≤60秒、任务执行总期限120秒、排队5分钟过期；首版并发1。provider/model/base_url/模板/输出上限变化时，旧任务发送前以 configuration_changed 失败，释放额度。

未绑定上传24小时；成功历史及图片30天；失败/unknown图片终态后24小时。到期历史先隐藏再清正文；图片保留期按全部有效引用最大值，活动租约保护对象。执行中删除不会复活结果，也不能保证取消供应商计费。点击删除后新读取立即404，物理对象由维护循环清理；此前签名URL在到期或实际删除前可能仍有效。

当前墓碑/安全计费元数据保留以维护幂等和限额；第三方保留规则、备份删除及长期元数据归档策略上线前另行核实。维护异常只记录任务/资产ID和安全错误类型，可从任务状态、error_code及attempt查证；不依赖HTTP健康200证明模型服务可用。

## 验证与尚未验证

真实 PostgreSQL/Redis 测试只运行在专用库，测试各自创建并清理随机schema与Redis命名空间：

```bash
AIGC_TEST_DATABASE_URL=postgresql+asyncpg://wangxinlin@127.0.0.1:5432/aigc_image_prompt_test_20261022 \
AIGC_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
uv run pytest -q
```

自动测试中的 COS transport 和视觉供应商替换成本地对象/HTTP transport；核心查询、事务、锁、JWT、Redis Lua、图片解码及SDK协议真实执行。另有真实 COS 小图验证：私有读取字节一致、无签名GET 403、签名GET 200且内容一致，测试对象已删除。尚未完成微信新AppID或真实模型效果验收。新网关配置通过规则测试，但本机没有Nginx/Docker，尚未做真实Nginx语法/公网验收。

上线前仍需：配置新AppID和域名、恢复百炼账户并用选定模型做20图质量/单价评测、核验生产环境COS策略与配置、审阅生产迁移，再联调网站BFF与小程序真机。模型密钥不应发在聊天中。

### 本次本地验收记录

Qwen 3.8-Max 接入后的最新回归：全量 `659 passed`；新增及关联模块覆盖专项 `238 passed`，行 `93.09% (1118/1201)`、分支 `82.54% (208/252)`。原“缺存储配置”测试显式清空各个必填项，避免读取本机真实COS配置后产生假失败；无真实供应商调用混入自动测试。以下初版验收保留为实施过程记录。

- Python 3.13.11，真实 PostgreSQL 16.15 与 Redis；全量 `632 passed`，无跳过。
- 新增及关联模块覆盖专项 `211 passed`：行 `92.88% (1109/1194)`，分支 `82.14% (207/252)`，包含原有 auth router。不是全仓所有业务的覆盖率。
- 覆盖工具：设置上述测试环境变量后运行 `uv run --with coverage==7.16.2 python tests/prompt_analysis_coverage.py`。报告位于 gitignored `htmlcov/prompt-analysis/coverage.json`；仅在测量进程采用 SQLAlchemy 自带 Python 实现、greenlet/thread tracing，以及 asyncpg 同步 Cython 构造的兼容包装，不排除业务代码。
- 真实HTTP业务集成32项通过；三张新表在隔离库验证升级/回退，完整迁移链在专用测试库升级至 `e04b75ad6312`。
- 实际 Uvicorn socket smoke（lifespan关闭以避开无关定时器）：health、示例、OpenAPI返回200，私有匿名请求401且统一JSON/no-store，新登录/上传/任务路由已注册。仅证明本地路由可达。
- `uv run ruff check app tests deploy/mini-api-gateway/release.py` 与 `git diff --check` 通过。`ruff check .` 仍发现未修改的旧 `deploy/inspect_login.py` 中两项导入问题，没有为本功能改动该调试脚本。
- 新增回归实际复现并修复：删除与Worker派发竞态、READY提交失败后的图片补偿、COS私有签名DEBUG日志、缺模型配置时排队过期维护、异常multipart错误归类。无付费模型调用、生产迁移、推送或部署。

## 2026-10-26：文字想法生成提示词（开发完成待联合验收）

新增“帮我写提示词”，按用户批准的双模式首页与生成结果/手动编辑设计实现；本轮不实现聊天、多轮改写或图片生成。技术栈沿用 Python 3.13、FastAPI、Pydantic、SQLAlchemy async、PostgreSQL、Alembic、Redis 和既有 OpenAI 兼容/DashScope provider。图片上传继续使用私有 COS；文字任务不创建、读取或签名图片资产。

`POST /api/prompt-analyses` 兼容原来的 `{"asset_id":"UUID"}`，默认 `input_type=image`。文字请求示例：

```json
{"input_type":"text","idea_text":"雨夜送花的小猫，做一张手机壁纸","purpose":"wallpaper","style":"cinematic"}
```

- `idea_text` 去首尾空白后 1–500 字符；`purpose/style` 可省略或为 null。用途为 wallpaper/avatar/poster/product/illustration；风格为 cinematic/photorealistic/illustration/anime/minimalist。
- text 请求不接受图片资产；image 请求不接受想法或选项。输入在 Pydantic 与数据库约束层校验，额外字段拒绝。
- 原路由、JWT、私有所有权、频率限制、每日额度、单个在途任务、队列上限、幂等及编辑 revision 协议复用。文字幂等摘要包含原始想法及两个选项；同键不同输入返回冲突，网络重放不重新生成。
- 详情和历史额外返回 `input_type/idea_text/purpose/style`。text 的 `asset_id` 和预览链接为 null，`image_available=false`；不是图片过期。
- Worker 对 text 使用固定 `text-v1` 模板，发送文字 JSON 数据，生成原有严格 `ModelResult` 的中英提示词与主体/构图/光线/风格创作要点。复用现有供应商配置和安全用量统计，不使用图片消息。无自动供应商重试；dispatch intent、租约栅栏和 unknown 保守恢复继续生效。
- 文字输入和生成结果是当前账号的私有历史；服务端确认成功后才显示已保存。删除及到期清理原想法、用途、风格和生成/编辑正文，保留必要幂等与用量墓碑。编辑/语言切换/复制不调用模型。

迁移 `f15c86be7423` 的父版本为 `e04b75ad6312`，新增输入字段、互斥约束并将 `asset_id` 改为可空；旧记录默认 image。存在任何文字任务时拒绝 downgrade，避免丢失历史和计费墓碑。自动迁移白名单没有扩大；生产发布前仍须审阅备份、确认当前版本，先执行迁移，再同步更新 API/Worker，最后发布小程序。不能先发布新前端却保留旧的必填图片请求接口。

本轮测试使用隔离 PostgreSQL schema、命名空间 Redis 和本地模型 HTTP transport，包含 API→Worker→SDK 协议→结果落库→编辑→历史→删除链路。未额外调用付费模型，没有执行生产迁移、推送或部署。最终测试/覆盖率及微信验收数字由本轮验证记录提供；不能将本地替身等同真实模型质量或真机验收。

## 2026-10-26 网站同页聊天契约

此节在前述文字能力基础上扩展网站多轮创作，不替换小程序单次分析接口。沿用Python/FastAPI、PostgreSQL/SQLAlchemy/Alembic、Redis及现有私有COS与OpenAI兼容/DashScope供应商，独立chat-v1模板。

### HTTP

所有会话入口使用原JWT鉴权、统一success/data/error/request_id封套、private/no-store响应和有界JSON体。创建/续写/重试需要UUID格式Idempotency-Key；首次返回202，完全相同的安全回放返回200，不重复计费。

| 接口 | 输入/响应 |
| --- | --- |
| POST /api/prompt-conversations | body `message?:string(1–500)`、`asset_id?:UUID` 至少一项。返回当前AnalysisDetail。图片可带文字要求。 |
| POST /api/prompt-conversations/{id}/messages | 上述输入加 `parent_analysis_id:UUID`、`parent_revision:int>=0`。父任务必须是同一会话当前最新且成功的一轮。返回新AnalysisDetail。 |
| POST /api/prompt-conversations/{id}/retry | `analysis_id:UUID`，仅允许主动重试当前最新failed/canceled任务，复用其原输入与冻结上下文。unknown只查询与恢复，不准重调。 |
| GET /api/prompt-conversations | limit默认20、最大50及cursor；返回items和与原历史同形pagination。每项为id/title/status/created_at/updated_at/latest_analysis_id/version。 |
| GET /api/prompt-conversations/{id} | 返回id/title/created_at/updated_at及按版本顺序的turns。每轮是完整AnalysisDetail；最多100轮。 |
| DELETE /api/prompt-conversations/{id} | 整组软删除并清私密原文/上下文/结果，204；额度、幂等与派发墓碑保留。 |
| POST /api/prompt-assets/from-prompt | 仅 `prompt_id:UUID`，201返回原AssetDTO。服务端检查实际公开作品首图，受控复制至当前用户的私有参考资产。 |

旧GET/PATCH /api/prompt-analyses/{id}继续用于轮询和人工编辑，保持旧行为，额外返回 `conversation_id`、`parent_analysis_id`、`user_message`、`version`。user_message始终字符串（纯图片为空字符串）；旧分析的conversation_id归一化为自身id、version=1，用户可从旧结果继续创作。会话title始终非空、最多80字。客户端不传模型上下文、提示词快照或任意图片URL。

旧GET /api/prompt-analyses历史排除网站chat轮次，保留原image/text单次记录；旧单轮DELETE遇会话根或子轮、或已有chat子轮的legacy根，返回409 `CONVERSATION_MANAGED`，不删除内容。用户需在会话历史明确整组删除，避免静默扩大旧操作影响，也防止删除失败轮后从更早成功版绕过retry约束。原纯单次分析删除不变。

新增安全冲突包括409 CONVERSATION_CHANGED、PARENT_REVISION_CONFLICT、NOT_RETRYABLE、CONVERSATION_UNKNOWN、CONVERSATION_MANAGED，达到100轮返回429 CONVERSATION_LIMIT。原有认证、额度、存储、队列等错误保留。受控图库源不受支持返回422 SOURCE_IMAGE_UNAVAILABLE，读取暂不可用返回503 STORAGE_UNAVAILABLE。

### 数据与隐私

PromptAnalysis增加conversation_id/parent_analysis_id/conversation_turn/user_message/context_snapshot；每轮仍是原耐久任务，根任务ID就是会话ID，复合外键确保同所有者，无额外消息表重复结果。上下文仅在服务器从父版当前有效中英提示词（包括人工编辑）、拆解和最多6条近期要求构建，再冻结供本轮模型使用；不把别的账号、签名URL、凭据交给模型。切语言、人工编辑、复制、重开历史均不调用模型。

创建、编辑、整组删除遵守budget→asset（存在时）→job锁序；Worker仍在外部调用前持久化dispatch_intent，租约失效/删除不复活结果。保留共享单在途与每日预算；unknown不自动重发。整组各轮统一继承根任务原有30天留存截止，追加、重试、完成均不会延长该截止；到期隐藏后清整组user_message/context_snapshot、原想法及生成/编辑正文，避免不可见的子轮私密数据超期保留。删除同样清这些字段。旧单次分析留存行为不变。会话默认私有，不自动进入图库或投稿。

图库带图只按数据库公开Prompt首张原图映射已配置COS/CDN的prompts/、submissions/或真实图库的library/对象；拒绝外部URL、查询串、非https、用户名密码、异常端口、路径穿越、私有prompt-analysis前缀。SDK从配置桶读取最多5MiB，再复用实际解码/去metadata/私有重编码及上传限频；不依赖浏览器跨域下载或接受任意地址。

### 迁移与验证

新head27f86a5d9c10接f15c86be7423。先审阅执行迁移，再同步API/Worker，最后更新网站；没有扩展自动迁移白名单。存在会话记录或计费墓碑时拒绝降级，避免隐式丢失上下文与版本历史。

本地780全量测试通过；56项新增、344项关联coverage专项通过。专项行94.01%、分支85.76%（不是全仓）。留存回归包含跨deadline创建/完成不续期以及整组到期清理。使用隔离PG schema、命名空间Redis、COS替身和真实SDK的本地HTTP transport；未调用真实模型、执行生产迁移、提交或部署。真实模型效果/费用与正式网关发布验收仍需单独授权并记录。

### 本地网站连接新版真实创作服务（2026-10-27）

本地网页不代表后端也在本地。公开图库与Google登录仍由 `127.0.0.1:8000` 的既有SSH转发提供；创作BFF通过服务端 `PROMPT_CREATION_API_BASE_URL=http://127.0.0.1:8040` 连接本机新版API与Worker。普通图库、登录和收藏仍沿用 `API_BASE_URL`，不得改成测试fixture。

运行状态使用独立本地库 `aigc_prompt_preview_20261027`，精确迁移至 `27f86a5d9c10`；Redis专用逻辑库12。不要对线上数据库、旧开发库执行这套预览迁移，也不要因为独立SEO迁移分支而改用无明确目标的 `upgrade head`。创作记录保留在本地，不自动同步到正式环境。

```bash
# 数据库已创建并完成迁移后，在后端目录启动API与Worker。
.venv/bin/python -m scripts.run_local_prompt_preview \
  --database-url postgresql+asyncpg://wangxinlin@127.0.0.1:5432/aigc_prompt_preview_20261027 \
  --redis-url redis://127.0.0.1:6379/12 \
  --model-config-from-ssh /tmp/prompt-bulk-ssh.NJSClJ/control
```

SSH路径仅为本次已核对所属用户、0700目录与远端身份的现有连接，重启后需重新确认。启动器只从固定主机/容器读取六项既有视觉模型配置，捕获到进程内供API与Worker使用，不打印、不写入文件、不读取生产JWT密钥。启动器不迁移数据库、不自动提交生成任务；任一子进程退出会回收另一子进程。Ctrl+C可停止本任务API/Worker，不会关闭借用的SSH master。

ASGI入口 `scripts.local_prompt_preview:create_app --factory` 只在显式预览标记、本地预览库前缀、loopback Redis非零库及8040端口合同下启用，不导入生产app、不运行采集/SEO scheduler。每个私有请求先由真实 `/api/auth/me` 验证，再将可信UUID及必要昵称/头像镜像到本地；不保存JWT、邮箱或Google身份字段。图库带图只从固定真实公开详情接口读取原作品，验证同配置COS桶及允许目录，再复用原私有上传/任务业务，不接受任意URL。

前端目录启动命令：

```bash
API_BASE_URL=http://127.0.0.1:8000 \
PROMPT_CREATION_API_BASE_URL=http://127.0.0.1:8040 \
AIGC_DEMO_MODE=false NODE_OPTIONS=--dns-result-order=ipv4first \
npm run dev -- --hostname localhost --port 3137
```

匿名本地私有接口应返回401；授权后的历史请求应返回真实本地记录。健康与OpenAPI只证明服务运行/路由注册；真实模型调用与生成完成需另记验收，不能以空历史200代替。

## 网站普通对话（2026-10-28，本地接入）

网站原会话接口新增可选mode：auto（默认）与generate。首页与侧栏“发送”统一使用auto，避免首页问候被强制生成；API显式generate仍保留兼容。普通问候、闲聊和咨询由真实模型普通回答，创作/修改请求输出现有中英提示词；不增加第二次分类请求、联网工具、Agent框架或假流式。

结果仍在现有JSONB持久化，数据库不新增迁移。模型输出reply-1严格包含schema_version、title（1–80字符）和reply（1–4000字符）；生成结果仍为原ModelResult。普通正文可随会话重开/删除，但不能通过提示词编辑接口修改成提示词。旧单次分析仅允许ModelResult。

会话详情每条消息新增三个字段：

| 输出 | response_type | assistant_reply | prompt_number | result / prompt |
| --- | --- | --- | --- | --- |
| 普通回复成功 | assistant | 完整正文 | null | null / null |
| 提示词成功 | prompt | null | 从1开始的提示词序号 | 原结构 |
| 尚未成功 | null | null | null | null / null |

version仍是排序和并发使用的消息轮次，不等于提示词序号；旧无新增字段的消息仍可兼容读取。并发parent指向最新成功消息，生成上下文由服务端向前取最近实际提示词及其人工编辑，再附最多6条近期双方对话。强制生成模式和近期上下文随幂等请求/主动retry冻结；未知发送仍只查询，不重新调用。

普通回复继续使用原每日模型调用配额、频率限制和单在途规则。根任务30天留存截止、私有no-store、整段删除、到期清理、所有权与dispatch intent保护保持。chat-v2启用前需处理旧chat-v1在途任务；配置不匹配不自动重发。

本次验收是398项隔离自动测试和网站桌面/手机混合会话走查。测试provider/COS素材只用于隔离测试，不是用户预览或真实模型效果证明；实际模型路由质量需单独获准后验收。本地真实预览仍由8000真实图库/认证、8040创作API/Worker提供；前端仅服务端PROMPT_CREATION_API_BASE_URL指向8040。


### 2026-10-28 个人每日额度

默认PROMPT_ANALYSIS_DAILY_LIMIT为10（原5）；示例配置同值。本地8040已重载并显式使用10。闲聊、生成、修改继续共用每日额度，北京时间自然日统计；当日已占用不重置。全站PROMPT_ANALYSIS_GLOBAL_DAILY_LIMIT仍100。环境变量显式配置会覆盖默认值；本次未更新线上服务。
