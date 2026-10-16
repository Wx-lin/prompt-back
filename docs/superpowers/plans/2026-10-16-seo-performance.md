# SEO implementation and search performance

Goal: expose approved AI提示词/文生图 keyword landing pages and a truthful admin search performance view for aigc-prompt.com.

Architecture: Next.js renders keyword/category content and crawlable pagination. FastAPI stores authenticated, bounded search-platform reporting snapshots. Admin reuses existing form, table, ECharts, and authentication components. No new UI library. Google/Baidu account verification remains user-owned; until then show not connected, never invented zero metrics.

- [x] Frontend: test homepage/category metadata, actual page pagination, detail alternates/JSON-LD, complete sitemap; implement and run lint/typecheck/tests/build.
- [x] Backend: test snapshot validation, idempotent replacement, overlapping-window rejection, isolated dimensions, auth and database persistence; implement explicit migration and run unit/integration checks.
- [x] Admin: test exported CSV parsing (quoted commas/newlines/BOM, translated headers, absent position, invalid metrics). Implement engine/window filters, coverage status, totals only from daily dimension, trend chart, independent keyword/page tables, CSV preview then explicit import, verification guidance.
- [x] Review security/correctness and run browser against isolated local test service. Record actual results and unverified production actions.

Data: Google reports source days in America/Los_Angeles; preserve date labels rather than converting them to Beijing days. Query/page aggregates are separate views and must never be added to site daily totals. No search-volume or ranking guarantees. Manual imports clearly display updated_at and coverage; no automatic-sync claim.

Implementation remains local; no commit, push, migration of production database or deployment in this scope.
