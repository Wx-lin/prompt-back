# Admin dashboard and users implementation plan

Approved design: ../specs/2026-10-16-admin-dashboard-users-design.md. User approved 2026-10-16. Read-only feature, no production data mutation or schema change.

- [x] Backend: new admin_insights service/schema/router mounted in main; inherited admin-key authorization on both GET endpoints; write failing tests first, then implement aggregated counts and filtered stable user pagination.
- [x] UI: DashboardPage and UsersPage, shared insights API/types; default /dashboard route and menu, preserve existing routes; loading/error/refresh and no fabricated zeroes.
- [x] Tests: real isolated PostgreSQL service integration (timezone today, public boundary, unknown and zero categories, literal search, pagination/privacy), API authentication/parameter guards and frontend utilities; browser mocked and real local API verification.
- [x] Review: independent review and relevant lint/typecheck/test/build, record checks and deployment boundaries. Preserve existing uncommitted incident documents.

Contract GET /api/admin/dashboard: {generated_at,users:{total,enabled,disabled,today_new},content:{total,published,pending,rejected,other_unpublished,images},crawl:{total,pending},categories:[{key,label,total,published}]}. pending/rejected count nonpublic rows for mutually exclusive partition. Categories canonical12 plus unknown bucket key __other__ label 其他/历史分类 when nonempty. Timestamp UTC ISO; today_new uses Beijing local day [start,nextstart).

Contract GET /api/admin/users?search=&is_active=true|false&page=1&page_size=20: {items:[{id,nickname,email,is_active,created_at,last_login_at}],total,all_users_total,page,page_size}. search max200 trimmed, literal substring nickname/email, stable created_at desc,id desc. page>=1,1<=page_size<=100; no identities/tokens exposed. All endpoints administrator only.

## Verification (2026-10-16)

- Full current workspace backend suite: 241 passed with explicit local PostgreSQL test URL, isolated random schemas. Ruff passed. Includes concurrent unrelated model-catalog tests; those files are outside this feature.
- Admin: 11 unit tests passed, TypeScript/Vite build passed, lint passed with six pre-existing warnings.
- New backend feature coverage reported by implementer: router/schemas100%, service97%, combined99%.
- Real browser via Vite same-origin proxy to real isolated FastAPI/PostgreSQL: default dashboard route, totals, canonical plus historical category bucket, null user fields, paging, search literal wildcards, status filtering, reset, 503 retry, 401 login redirect, mobile render all passed.
- Reproduce: `AIGC_TEST_DATABASE_URL=postgresql+asyncpg:///postgres .venv/bin/python -m uvicorn tests.admin_insights_e2e_app:app --host 127.0.0.1 --port 8000`; admin `npm run dev -- --host 127.0.0.1 --port 5180 --strictPort`; `.venv/bin/python tests/admin_insights_browser.py`. Fixture creates and drops its own schema.
- Screenshots and browser check results are under workspace `artifacts/admin-insights-2026-10-16/`, using disposable fixture data, not live counts.
- Independent security/code review found no blockers. No production user changes or database migration. Not committed, pushed or deployed; deployment requires explicit authorization.

## ECharts visual redesign verification

User rejected the initial plain table layout and explicitly requested ECharts. Implemented actual ECharts6.1 grouped category bars and status donut; shared sidebar, KPI cards, user directory panels and mobile icon rail. No fake trend data. Pure chart option tests verify counts/ordering/zero data. Modular chart code is lazily loaded; Vite reports its 536KB chart chunk (182KB gzip), rather than bundling it into the main entry.

Admin13 tests passed; build and lint pass (existing warnings). Actual local browser checks confirm SVG charts, resize without mobile page overflow, refresh/navigation remount, zero inventory0%, no runtime exceptions, and existing database-backed user filtering/pagination. Visual screenshots dashboard-echarts.png/users-echarts-style.png use explicit test preview data; not production counts. Not deployed.

用户已于2026-10-16明确回复“部署吧”，授权提交与部署；发布过程以对应提交的 Actions 及线上真实接口/浏览器验收结果为准。
