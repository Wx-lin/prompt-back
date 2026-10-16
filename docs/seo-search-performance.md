# SEO 搜索表现数据

后台搜索表现来自搜索引擎站长平台的实际报表，不能用站内搜索、浏览数或爬虫访问量替代。
当前提供人工导入，不会自动连接 Google/Baidu，也不会从任意外部 URL 抓取数据。
需要站点所有者先在 Google Search Console、百度搜索资源平台验证域名并取得报表权限。
Google 自动同步的服务账号/OAuth 授权与百度账号尚未配置，界面必须明确标记。

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
