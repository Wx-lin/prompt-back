# 12 分类升级：本地验收与生产执行说明

范围依据 `docs/aigc-prompt-分类优化需求.md`。当前完成本地目录、入口、校验、兼容查询和分类迁移工具；生产未部署，逐条建议仍未确认。历史快照、图文复核与逐条处理建议存放在工作区 `artifacts/category-review-2026-10-14/`，私有权限，不放进公开站点或Git仓库。

## 当前交付

- `app/utils/category_catalog.py`：12类双语、顺序、收录边界，主站通过 `/api/categories` 接入。前端保留故障兜底与双语映射，以目录一致性测试约束；后台仅使用API目录作为可写选项。
- `app/utils/public_prompts.py`：`is_published=true AND (review_status IS NULL OR review_status='approved')`。分类计数与公开查询共享。
- 新迁移 `9c35e1a62f4`，父版本 `f8b24d0c51e3`：旧分类集合与变更日志表，无自动数据迁移。
- `scripts/category_migration.py`：默认工作流为离线预演；仅明确 `confirmed=true` 行可应用，只改分类字段，不混改正文、图片和发布状态。批次变更日志与兼容集合在同一数据库事务提交。

## 离线预演

从后端目录运行；审阅清单必须保留完整实体ID、旧分类和确认状态。AI建议不等于人工确认。

```bash
.venv/bin/python3 -m scripts.category_migration preview \
  --snapshot ../artifacts/category-review-2026-10-14/snapshot.json \
  --review ../artifacts/category-review-2026-10-14/review-list-v2.json \
  --output ../artifacts/category-review-2026-10-14/preview-new.json
```

输出文件不得已存在；通过0600、独占创建避免覆盖审计结果。当前202行未确认，预演0条可应用。`unresolved_public` 是预演后仍为旧key的公开数量，与“没有分类建议的23条”不是同一口径。

## 正式执行前置

1. 运营审核95条正式内容及11条草稿候选，并确认23条已发布待处理内容的状态处置；当前提案为暂时下架保留，尚未授权。不能自动确认、自动拆分、自动下架或将教程塞入风景静物。
2. 明确授权生产迁移和发布；重新只读导出实际数据，核对新增记录和审核期间的编辑。先完整数据库备份，确认后端/主站/后台的发布和回滚版本。
3. 审阅并执行Alembic结构迁移。不得扩大自动部署迁移白名单以绕过审阅。生产应用包含旧ID兼容集合、目录和校验，须配套发布。
4. 将人工确认落实到清单的逐行 `confirmed` 布尔值，保持未确认行false。先预演；正式切换前所有仍公开的旧分类必须解决。

配置数据库凭据通过既有环境/密钥管理传入，不能写进命令、脚本、清单或报告。在线命令要求显式目标 `host:port/database` 与配置一致：

```bash
.venv/bin/python3 -m scripts.category_migration export \
  --expected-target '<实际数据库host:port/database>' --output '<新的只读快照路径>'
.venv/bin/python3 -m scripts.category_migration apply \
  --snapshot '<已审阅快照路径>' --review '<已确认清单路径>' \
  --batch-id '<本次唯一批次名>' --expected-target '<实际数据库host:port/database>' \
  --output '<新的执行日志路径>'
.venv/bin/python3 -m scripts.category_migration rollback \
  --batch-id '<需要回滚的批次名>' --expected-target '<实际数据库host:port/database>' \
  --output '<新的回滚日志路径>'
```

工具以记录锁比对实际正文、模型、作者来源、分类、审核状态及图片；并发修改跳过报告。通用updated_at也受互动更新影响，因此不作为编辑版本；点赞、浏览和收藏不影响分类应用/回滚。回滚仅恢复分类且比对迁移后内容，人工编辑过的行跳过。兼容集合保留，不重新公开已下架内容。已回滚批次不能重复应用，应重新审核并使用新批次名。

发布状态变更走既有后台操作，需单独保留原状态并验收投稿图片私有ACL；本工具拒绝夹带 `is_published/review_status/status` 变更，不负责COS状态回滚。

## 本地验证

真实数据库检查使用本机PostgreSQL的随机独立schema，不修改public或生产。后端全量测试及迁移集成覆盖分类、审核可见性、跨类旧集合与组合分页、重复应用、正常互动不误报、正文编辑跳过和安全回滚。目录迁移模块有单独覆盖率报告。

主站/后台真实API浏览器fixture：

```bash
AIGC_TEST_DATABASE_URL='<本机隔离测试连接>' APP_ENV=test \
  .venv/bin/python3 -m uvicorn tests.category_e2e_app:app --host 127.0.0.1 --port 8000
```

另启动后台Vite5176（代理8000），主站使用 `API_BASE_URL=http://127.0.0.1:8000 AIGC_DEMO_MODE=false npm run dev -- --port 3027`。在主站目录执行：

```bash
AIGC_ADMIN_E2E_URL=http://127.0.0.1:5176 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3027 \
  npm run test:e2e -- e2e/categories.spec.ts e2e/admin-categories.spec.ts --workers=1
```

这套fixture只用于本机验收，启动时建独立schema、正常退出时删除；不能部署tests模块。浏览器测试会在隔离库审核测试投稿。覆盖桌面/手机、双语/深浅、全部分类数量、零库存、旧值编辑、保留筛选和投稿审核改类。

## 上线验收

重新核对分类数量与公开列表之和、旧五类链接和三类兼容集合、直接详情URL与刷新、投稿12类校验及后台审核。确认任何入口都不暴露非公开内容。记录所有跳过项与原状态，不能仅凭容器运行宣称发布成功。
