# AI 创作 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 AI 创作对话中接入 LangChain Agent，让它理解平台能力、进行多轮对话并通过受控工具选择提示词、图片分析和图片生成。

**Architecture:** Agent 运行在现有 worker 的任务边界内，使用原生模型工具调用并返回受校验的动作；图片生成、图片权限、额度和幂等仍由现有服务执行。新旧任务按冻结的版本分别运行，保留现有 API 和历史数据格式。

**Tech Stack:** Python、FastAPI、Pydantic、LangChain、LangGraph、OpenAI 兼容接口、Redis、PostgreSQL。

---

## 任务

- [x] **测试 Agent 行为。** 在 `tests/test_creation_agent.py` 使用模拟模型的真实工具调用消息，覆盖能力咨询、直接聊天、提词、生图、参考图、多个或错误工具调用、模型/工具失败和调用上限。
- [x] **实现 Agent。** 在 `app/services/vision/agent.py` 定义受限工具及模型循环，携带最近对话和当前图片；结果转为现有 `VisionResult` 和 `ConversationImageIntent` 等类型。生图工具只输出待执行意图，不直接调用付费图片服务。
- [x] **兼容在途任务。** 为新 smart 请求冻结 `agent-v1`，保留 `chat-v2` 的旧执行路径；修改 `app/services/prompt_analysis/api.py`、`jobs.py`、`app/workers/prompt_analysis.py` 和相关测试。用 `uv run pytest -q tests/test_prompt_jobs.py tests/test_smart_conversation.py` 核验新旧版本、删除/取消、图片归属与扣费围栏。
- [x] **依赖与发布配置。** 在 `pyproject.toml` 添加所需 LangChain 依赖并同步 `uv.lock`、`deploy/requirements.txt`；验证百炼 OpenAI 兼容配置、超时、关闭自动重试和模型/工具次数限制。
- [x] **自动化验证。** 运行相关测试、后端完整 pytest、Ruff 和部署依赖一致性检查。真实模型工具调用尚未验证；不向模型供应商发起未经授权的付费调用。
