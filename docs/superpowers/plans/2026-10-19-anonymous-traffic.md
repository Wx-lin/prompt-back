# 匿名UV implementation plan

Design approved by user request following UV explanation: ../specs/2026-10-19-anonymous-traffic-design.md.

- [x] TDD backend model/additive migration, daily idempotent insert and exact range distinct counts, admin-only metrics,90day retention.
- [x] TDD Next same-origin BFF, secure anonymousCookie hashing and best-effort invisible tracker, rootmount and privacy disclosure.
- [x] TDD admin traffic API/UI, ECharts daily UV bars,7/30switch, timezone and approximateperson caveat, separate failure handling.
- [x] Isolated Postgres + route/cookie/client + real browser validation; independent securityreview, fullchecks/build. No production changes without explicit deployment approval. Never backfill imaginary visits.

## Validation (2026-10-19)

- Real local PostgreSQL traffic tests:3 passed; related backend regressions:29 passed; Ruff passed.
- Frontend full190 tests, lint/typecheck/build passed; post-review9 focused tests passed (96.61% coverage).
- Admin full tests/lint/build passed (existing lint/chunk-size warnings remain).
- Real Chromium: anonymous HttpOnly cookie across repeated requests,2 independent contexts =>2/2/2 UV; admin SVG bars and30day toggle verified. Existing admin browser regression passed, including mobile, user filters and401/503 handling.
- Preview: ../../../../artifacts/traffic-dashboard-2026-10-19.png (fixture data only).
- Local browser verification used /en; existing local production-mode Chinese-root locale redirect loop prevented / verification. No production homepage claim made.
- Python3.13 coverage tracing with asyncpg segfaulted; normal database tests pass. Backend DB integration coverage not established.
- Deployment not performed. Requires explicit authorization and reviewed additive migration c74d219e8a60 before activating both services. Automatic migration whitelist unchanged; explicit reviewed dispatch supports c74d219e8a60 only from a3e6b18d4092.

User authorized deployment. Pre-push backend258 passed/11 skipped, frontend191 passed + lint/typecheck/build; dispatch gate42 tests passed.
