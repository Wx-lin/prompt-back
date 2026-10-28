# 网站普通对话 Implementation Plan

**Goal:** 网站侧栏支持真实闲聊、咨询和连续讨论；明确生成或修改时仍返回现有双语提示词。

**Architecture:** 复用 FastAPI、现有 PostgreSQL 耐久任务/私有历史和 Worker，由同一次模型调用返回严格提示词结果或普通回复。普通回复存入现有 JSONB，不新增表、数据库列或 AI 框架。网站复用现有聊天组件、主题、登录和 BFF；不修改小程序或生产部署。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy/PostgreSQL、Redis、OpenAI-compatible provider；Next.js 16.4.2、React、TypeScript、next-intl、Base UI、Vitest、Playwright。

## 已确认的行为与契约

- 用户在 2026-10-28 明确要求“网站这块聊天增加个闲聊吧先”，沿用本聊天已讨论的普通问答/生成分流方案。
- 首页与侧栏发送统一默认 `mode=auto`（修正首页强制generate导致问候生成卡片的问题）；API显式 `mode=generate` 仍保留兼容。意图冻结进幂等请求和服务器任务快照，登录恢复和主动重试保持它。
- 普通回复模型结果为 `{schema_version: 'reply-1', title, reply}`，标题非空至多80字符，正文非空至多4000字符，额外字段拒绝；旧 ModelResult 不变。
- 会话 API 将普通回复投影为 `response_type=assistant`、`assistant_reply`、`result/prompt=null`；普通回复没有提示词编辑/中英切换。提示词结果保持原字段，另给 `prompt_number`；`version` 始终是消息顺序。
- 续写 parent 始终指向最新成功消息并校验 revision；生成上下文另回溯最近真正提示词，包含人工编辑。最多6条近期 user/assistant 文本帮助理解“第二种”等指代，无历史提示词也能续聊。
- 不增加联网检索、工具调用、假流式或固定关键词自动话术。保留认证、所有权、额度、幂等、租约、dispatch intent、unknown只查询、整段删除/到期清理。

## 实施与验证

- [x] Provider/schema：先测试普通回复/强制生成/格式边界/单调用，再实现 `chat-v2`，仅会话允许回复联合类型。
- [x] Service/Worker：先测试纯聊天、多轮指代、提示词→咨询→修改、人工编辑上下文、旧接口拒绝reply、幂等mode冲突和retry意图，再接入结果持久化与上下文。
- [x] 网站：先测试严格BFF解析、纯文本渲染、混合版本、登录/重试意图，再扩展既有组件，不引新UI依赖。
- [x] 验证：隔离PostgreSQL schema/Redis命名空间；模型使用测试transport，覆盖率≥80%；相关回归、lint/typecheck/build及桌面/390px深浅主题浏览器走查。
- [x] 在既有 AGENT/PROGRESS 文档记录实际测试与本地/生产边界，不提交、推送、改生产数据库或调用额外付费模型。

## 运行命令

后端隔离测试使用 `AIGC_TEST_DATABASE_URL=postgresql+asyncpg://wangxinlin@127.0.0.1:5432/aigc_image_prompt_test_20261022 AIGC_TEST_REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/python -m pytest tests/test_prompt_casual_chat.py`。测试自动创建随机 schema 和 Redis 前缀，禁止清空共享数据库。

前端使用 `npm test -- src/features/prompt-creation src/lib/prompt-analysis`、`npm run lint`、`npm run typecheck` 和 `npm run build`。真实预览保留8040创作服务/8000真实图库与认证分流；隔离浏览器测试单独使用 fixture，不替换用户最终预览。
