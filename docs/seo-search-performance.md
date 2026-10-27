# SEO 搜索表现数据

后台搜索表现来自搜索引擎站长平台的实际报表，不能用站内搜索、浏览数或爬虫访问量替代。
提供人工导入和默认关闭的 Google 专用只读自动同步，不会从任意外部 URL 抓取数据。百度仍仅人工导入。
需要站点所有者先在 Google Search Console、百度搜索资源平台验证域名并取得报表权限。
Google 自动同步必须单独配置服务账号并授予属性访问权限；未配置时界面必须明确标记。

## API

所有接口沿用 `X-Admin-Key`，响应禁止缓存。

- `GET /api/admin/seo?engine=google&start_date=2026-10-01&end_date=2026-10-28`
- `POST /api/admin/seo/import`

导入 JSON：

```json
{"engine":"google","dimension":"daily","start_date":"2026-10-01","end_date":"2026-10-02","rows":[{"key":"2026-10-01","clicks":2,"impressions":20,"position":8.5}]}
```

engine 为 google/baidu；dimension 为 daily/query/page。
key 分别为 YYYY-MM-DD、搜索词、`https://aigc-prompt.com` 下的完整页面地址。
clicks/impressions 为非负整数，clicks 不大于 impressions；position 为源报表平均排名，大于等于 1 或 null。
CTR API 返回 0–1 比率。百度报表没有平均排名时传 null，不能用 0 替代。
单次 2 MB/10000 行，窗口最多 366 天，键不得重复。
同引擎、同维度、相同窗口重复导入为完整替换；不同窗口重叠返回 409，避免累加重复数据。
例如首次导入 10 月 1–28 日后，应继续重新导入相同窗口，或导入不重叠的 10 月 29 日起窗口。
不能直接另导入 10 月 2–29 日；运营需选择一致的固定窗口。

返回 totals 只汇总 daily，queries/pages 分别独立，禁止相加。
未导入的 totals=null；来源明确为零的 daily 行保留零。缺日期不填零，coverage 标记 partial。
query/page 仅使用完整包含于查询范围的已导入窗口，不能拆分月度关键词数据冒充日度数据。
coverage 说明 missing/partial/complete；complete 只表示导入窗口覆盖，不能代表站长平台披露了全部搜索词。
connection_status=not_connected 表示还未导入，manual_import 表示人工导入过，均不表示 API 已授权。
updated_at 是本地导入时间；平台数据本身可能延迟，不代表当天实时结果。

## 日期与指标口径

Google Search Console 日期按源报表的 Pacific Time (`America/Los_Angeles`) 保留，不转成北京时间。
Google 默认查询最后 28 个完整 Pacific 日；百度默认最后 28 个完整北京时间日。
百度导入仍需核实源平台报表时区并保留日期，API 以 `source_report_timezone` 提示口径待确认。

Google 的隐私匿名查询、维度聚合和导出行数限制会使关键词/页面列表之和不同于日期总量。
不得补造缺失词、将查不到当作无曝光，或把不同维度之和用作总流量。
平均排名是实际展示位置的加权平均，不是某词固定名次；仅对有完整 source position 的曝光行按曝光加权。
跨引擎数据单独查看。导入原始报表前要确认是同一站点属性、Web 搜索类型、相同国家/设备筛选；
当前标准化接口不记录额外筛选，运营应统一采用站点全量 Web 报表，避免混用。

## 验证记录（2026-10-16）

测试覆盖标准化数据校验、真实 PostgreSQL 空数据/导入/幂等替换/窗口冲突/维度独立、
未授权请求、非法 JSON、2 MB 上限、平均排名缺失与数值溢出。

- 初次测试命令 `uv run pytest` 因该环境 pytest 脚本入口缺失失败；改用 `.venv/bin/python -m pytest`。
- 代码审查发现平均排名 `1e308` 会在加权时溢出；先添加 `test_position_overflow_rejected`，实际 RED 为 `DID NOT RAISE`。
- 增加最大平均排名 1000000 后，13 项测试 GREEN。
- 命令：`AIGC_TEST_DATABASE_URL=postgresql+asyncpg:///aigc_seo_test_20261016 .venv/bin/python -m coverage run --source=app.schemas.seo,app.services.seo_service,app.routers.seo -m pytest tests/test_seo.py tests/test_seo_integration.py -q`
- 实测覆盖率：schema 98%、service 100%、router 94%，合计 98%。这不是全项目覆盖率。
- 使用全新独立本地数据库实际执行 `DATABASE_URL=postgresql+asyncpg:///aigc_seo_test_20261016 .venv/bin/python -m alembic upgrade head` 成功；唯一 head 为 `a3e6b18d4092`，前置 `9c35e1a62f4`。
- `tests/seo_e2e_app.py` 提供独立 schema 的本地浏览器测试 API；不读取线上数据。
- 搜索引擎账号授权、真实站长平台导出文件与线上部署均尚未验证。


## Google 自动同步（2026-10-27）

服务端固定 URL-prefix 属性 `https://aigc-prompt.com/`、Web 搜索类型和
`https://www.googleapis.com/auth/webmasters.readonly`；网站登录 Client ID 不复用。
需站点所有者先验证该属性、在 Google Cloud 启用 Search Console API，
把专用服务账号 email 添加为属性用户。私钥 JSON 只读挂载在服务器，禁止进仓库、数据库和浏览器。

环境变量（默认禁用，不因文件路径存在而擅自启用）：

```dotenv
GSC_ENABLED=false
GSC_CREDENTIALS_FILE=/run/secrets/gsc.json
GSC_SYNC_HOUR=6
```

调度时间按 Pacific Time；`register_seo_job(scheduler)` 注册单实例、合并错过的定时触发。
服务账号只允许 Google 标准 token endpoint；报表 API 地址固定，不跟随重定向。
单次网络请求 20 秒，429/5xx/网络失败最多 3 次；单份响应流式限制 20 MB；
每维度单日最多 50000 行，达到上限标记 truncated；整次任务最多 600 秒。

`POST /api/admin/seo/sync/google` 先将 running 和 last_attempt_at 落库，再返回 202 `{accepted:true}`，
后台执行；未配置/已有任务返回 409。`GET /api/admin/seo/sync/google/status` 返回同步状态。
两者沿用管理员鉴权、no-store，GET 报表也附加同样的 sync 字段：
configured、status、last_attempt_at、last_success_at、last_error、data_through、truncated。
状态可为 not_configured/idle/running/success/failed。
错误仅固定分类代码，不返回密钥、路径、Google 原始响应或异常文本。
运行状态超过 660 秒视为 interrupted，后续触发可恢复。

首次回补最近 28 天，默认查询截止 Pacific 日期减 3 天，且强制 `dataState=final`。
后续每次回刷前 7 天，并从上次 data_through 连续补至目标日期；
停机过久时每次最多新增 21 天，分批追赶，不跳过中间断档。
`data_through` 表示已查询至该日期；不代表该日存在曝光，空 rows 永不补造零。
Google 可能只披露部分主要行；未触发本地行数上限也不能宣称所有搜索词完整。

`seo_api_days` 按日/维度隔离保存自动数据；`seo_sync_state` 保存单个固定属性的同步进度。
PostgreSQL advisory transaction lock 防止多进程并发，所有拉取成功后再原子替换本批次，
任意维度失败保留之前所有成功数据。状态更新以 attempt timestamp 防旧失败覆盖新任务。
旧 `seo_snapshots` 手动导入保护不变；自动源首次成功后，Google 报表仅展示自动数据，
手动数据仍保留但不会与自动数据相加。手动导入不是对自动数据补数；百度保持独立人工来源。

新迁移 `b6f20a93d812` 基于已提交 `e04b75ad6312`，只新增 SEO 两张表，
不依赖工作区其他未提交迁移。发布应在隔离目录执行目标 revision，不能对工作区分叉 head 盲目 upgrade head。

### 新增验证

Provider 单元测试先得到 `ModuleNotFoundError` RED；实现后验证固定属性、只读 scope、token 超时、
分页、最终数据、空结果、上限、403、503 三次重试、非法响应及错误脱敏。
停机追赶测试先得到缺失 sync_window 的 RED，新增连续有界窗口函数后 GREEN。
真实独立 PostgreSQL 验证原子替换、重导入幂等、失败保留、并发拒绝、取消状态、自动/手动不双计、
API 空结果不伪造零；路由验证鉴权、未配置、忙碌、202 前准备和调度 opt-in。
实际独立测试库完整迁移至 b6，再降至 e04，再升级 b6 均成功。

本次最终验证命令：
`AIGC_TEST_DATABASE_URL=postgresql+asyncpg:///aigc_seo_sync_test_20261027 .venv/bin/python -m coverage run --source=app.services.google_search_console,app.services.seo_sync_service,app.routers.seo_sync,app.services.seo_service -m pytest tests/test_google_search_console.py tests/test_seo_sync_routes.py tests/test_seo_sync_integration.py tests/test_seo.py tests/test_seo_integration.py -q`。
结果 29 passed；新增/修改 SEO 业务范围 coverage 95%（provider 94%、sync 93%、router 100%、report 100%），不是全仓覆盖率。

## 外部同步模式（Google 网络不可达时）

设置 `GSC_ENABLED=true`、`GSC_SYNC_MODE=external`，服务器不保存 Google 私钥。
外部模式 `configured=true` 表示已启用外部接收，是否获得实际数据仍以 last_success_at 为准。
sync 状态新增 mode（server/external）、trigger_available。external 的 trigger_available=false，
后台手动同步 POST 返回固定 409 提示通过 GitHub Actions 工作流触发；服务器不注册 Google 拉取定时任务。
默认 `GSC_SYNC_MODE=server` 保持原有行为。

受控外部 runner 仅使用专用只读私钥访问 Google，经已有可信 SSH 通道写回服务器：

```bash
# 服务器：读取同步前沿，stdout 为 JSON
python -m scripts.seo_sync status
# Runner：首次省略 --data-through；后续传服务器返回的日期
python -m scripts.seo_sync export --data-through 2026-10-20 > seo-bundle.json
# 服务器：stdin 读 bundle，命令成功后 stdout 返回 status/data_through
python -m scripts.seo_sync import < seo-bundle.json
```

Runner 只需 GSC_CREDENTIALS_FILE，不要求数据库或服务器私钥；export 的属性、搜索类型、最终数据、
Pacific 截止日期仍固定，不能通过参数选择别的站点或任意日期。
JSON bundle version=1，总量上限 64 MB，最多 28 天，每天必须包含 daily/query/page 各一次；
每维度每天最多 50000 行。行指标、日期、重复键、页面 canonical 和来源配置重新严格校验，
不因 SSH 通道可信而跳过输入验证。原始凭据、access token 不进入 bundle。
import 仅启用 external 时可用，禁止未来目标日期；以服务器事务内保存的前沿校验连续窗口，
允许完全相同 bundle 重放，其他旧窗口返回 stale_bundle，不回退已成功数据。
通过同一 prepare_sync/run_sync 持锁和原子写入，失败保持旧快照。

stdout 仅输出报表或状态 JSON，固定安全错误分类写 stderr，失败退出码 1。
工作流不得打印 bundle 内容或私钥；采用防重入并发组，并校验 SSH host key 与发布版本。
工作流和 Secret 的授权/配置由单独发布流程管理。

外部扩展验证：先写 bundle 测试得到缺模块 RED，再实现；最终 44 项测试通过。
真实 PostgreSQL 覆盖导出→解析→导入→完全重放→旧窗口拒绝，旧成功数据保持不变。
external 模式测试证明服务器不调度、不触发出网，CLI 测试证明错误不输出上游异常文本。
新增/修改 SEO 范围覆盖率 94%，其中 bundle service 84%、bundle schema 93%、CLI 96%。

Runner export 阶段失败时尚未导入服务器，因此工作流需在 export 前保存 UTC 开始时间；
失败时通过可信 SSH 调用：

```bash
python -m scripts.seo_sync failure --attempt-at '2026-10-27T12:34:56+00:00' --code upstream_unavailable
```

code 仅安全错误白名单，不能传原始 stderr。日期须为带时区、近两日内的时间，不能超前一分钟以上。
服务器持同一事务锁；若存在更新的 attempt 或 success，返回 `{recorded:false}`，不会覆盖新成功。
否则只记录失败状态和尝试时间，保留所有已成功数据与 last_success_at，返回 `{recorded:true}`。
SSH 本身无法访问时无法回写，应以 GitHub Actions 失败记录为补充信号。
