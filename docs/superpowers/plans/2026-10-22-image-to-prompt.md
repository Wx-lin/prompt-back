# 图片转提示词后端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 网站与新小程序共用私有上传、可靠解析任务、中英结果、编辑及历史接口。

**Architecture:** 扩展现有 FastAPI 工程，PostgreSQL 保存任务及历史，独立 Worker 领取执行，私有 COS 保存标准图。Redis 只用于频率限制。没有配置模型时明确失败，不模拟真实成功。用户已授权开始实施，不涉及生产迁移、部署或付费模型调用。

**Tech Stack:** Python 3.13、FastAPI、SQLAlchemy、PostgreSQL 16、Redis、COS、OpenAI-compatible vision API。

设计依据：本聊天 outputs/backend-design/2026-10-22-image-to-prompt/README.md。隔离分支 feat/image-to-prompt-backend；原 checkout 的部署文档改动不复制、不修改。

## 共享接口约定

- ORM：`PromptAnalysisAsset`、`PromptAnalysis`、`PromptAnalysisAttempt`，位于 `app/models/prompt_analysis.py` 并从 `app.models` 导出。
- Asset：id/user_id/object_key/thumbnail_key/sha256/width/height/byte_size/state/unbound_expires_at/purge_after/created_at。
- Analysis：id/user_id/asset_id/idempotency_key/request_hash/source_client/status/phase/prompt_version/provider/model/config_snapshot/generated_result/edited_prompts/revision/error_code/quota_date/quota_state/lease_token/lease_expires_at/started_at/finished_at/deleted_at/history_retain_until/image_retain_until/created_at/updated_at。
- Attempt：id/analysis_id/attempt_no/dispatch_state/provider_request_id/usage/error_code/dispatched_at/finished_at。
- `ModelResult`：严格 Pydantic，字段与已提供 JSON Schema 一致。`CreateAnalysisRequest(asset_id: UUID)`；`EditAnalysisRequest(revision: int, prompt: BilingualPrompt)`。BilingualPrompt zh/en 非空白、有界。
- `prepare_analysis_image(body: bytes, content_type: str) -> PreparedImage`，冻结 dataclass，字段 `image_bytes/thumbnail_bytes/width/height/sha256`。
- Vision `VisionResult` 冻结 dataclass：`result: ModelResult, provider_request_id: str | None, usage: dict`；`VisionError(code, uncertain=False)`；`require_vision_configuration()`；`analyze_image(image_bytes: bytes, *, timeout: float) -> VisionResult`。生产只有真实兼容 API，没有 mock 配置。
- Jobs `claim_job(sessions)`、`dispatch_job(sessions, job_id, lease_token)`、`finish_job(sessions, job_id, lease_token, result=None, error_code=None, uncertain=False)`、`recover_jobs(sessions)`；精确签名由 Worker implementer 发主 agent，主 agent 只通过公开函数测试，不复制内部实现。
- Worker 只负责执行、恢复和清理；创建/历史/编辑/删除由业务 service 负责。双方只共享 ORM，不共享文件编辑。
- 私有错误 `AnalysisError(status_code, code, message)` 位于 `app/services/prompt_analysis/errors.py`，API route 转换新 envelope，旧接口不变。
- 全部设置由 root 加入 app/config.py；各 agent 不改该文件、main.py、.env.example、deploy/ 或 PROGRESS，避免并发覆盖。

## Task 1：基线与基础结构

- [x] 运行 `uv sync --locked --dev`、`uv run pytest -q` 记录原有通过/跳过情况。
- [x] 创建隔离 PG 测试数据库，只在此执行迁移；真实 Redis 使用独立测试键前缀，不 flush 现有库。
- [x] root 添加设置、错误和 HTTP 边界；先添加缺失功能的行为测试并观察失败。

## Task 2：身份（独立 agent）

- [x] tests/test_image_prompt_auth.py 先验证新固定登录入口、旧入口兼容、AppID隔离、cid兼容；运行观察失败。
- [x] 修改 wechat_auth.py、auth.py、jwt.py；禁止接受客户端任意AppID，不自动合并身份。
- [x] 运行新测试及 test_wechat_auth.py、test_jwt.py。

## Task 3：模型与图片（独立 agent）

- [x] tests/test_prompt_vision.py、test_prompt_images.py 先验证严格双语schema、损坏/多帧/EXIF/像素限制、未配置失败和 SDK 禁重试。
- [x] 创建 schemas/prompt_analysis.py、services/vision/、services/prompt_analysis_images.py；注入模拟 transport 仅用于测试。
- [x] 运行新测试，不调用付费API。

## Task 4：持久任务（独立 agent）

- [x] 新增真实 PG 事务测试：租约领取、发送前后崩溃、旧租约fencing、删除不复活和清理多引用。
- [x] 创建 ORM/增量 migration（parent d91a72f4b308）、任务执行 service、独立 Worker，遵守最多2 attempt、不盲重试原则。
- [x] 运行对应真实PG测试；不修改部署迁移白名单。

## Task 5：公共 API 与闭环（root）

- [x] 真实 PG/Redis HTTP 集成测试先验证上传→建任务→处理中→结果→编辑→删除、分页与越权404。
- [x] 创建资产/任务/历史服务和路由，配置新模块错误envelope、预解析体积限制及private no-store。
- [x] 测同键并发、额度并发、软删除/编辑冲突、Redis失败、预览刷新。
- [x] 新增精确 mini gateway 路由、上传6MiB、JSON128KiB及JSON错误；Compose解析worker profile独立启动，默认关闭功能。

## Task 6：审查与交付

- [x] 专项单元+真实PG/Redis集成+API闭环，新增代码行覆盖与分支覆盖至少80%；全量 pytest 和 Ruff。
- [x] 独立 spec review、code/security review；修复高优先级问题并复测。
- [x] 更新 docs/image-to-prompt.md、AGENT.md 与 PROGRESS：配置、启动、接口及未验证边界。
- [x] 不提交、不推送、不部署；交付实际代码位置、启动方法、测试证据和模型/AppID待配置项。
