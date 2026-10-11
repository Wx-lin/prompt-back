/**
 * 全局类型定义：与后端 Pydantic Schema 一一对应。
 *
 * 后端定义见 app/schemas/admin.py 和 app/schemas/crawl.py。这里手写一份而不是
 * 从 OpenAPI 自动生成，是因为接口数量还不多（20 个左右），手写更直观可控；
 * 等到接口数量涨到几十个、或者多人协作前后端频繁对不齐时，再换成
 * openapi-typescript 自动生成（后端已经导出了 docs/openapi.json 供这个用途）。
 */

/** 后台内容（Prompt），对应 AdminPromptOut */
export interface AdminPrompt {
  id: string
  title: string
  prompt_text: string
  description: string | null
  category: string
  models: string[]
  author_name: string | null
  source_url: string | null
  is_published: boolean
  view_count: number
  like_count: number
  collect_count: number
  created_at: string
  updated_at: string
}

/** 后台举报记录，对应 AdminReportOut */
export interface AdminReport {
  id: string
  prompt_id: string
  prompt_title: string | null
  reporter_email: string | null
  reason: string
  status: string
  handled_note: string | null
  created_at: string
  handled_at: string | null
}

/** 采集草稿的配图，对应 CrawlDraftImageOut */
export interface CrawlDraftImage {
  id: string
  image_url: string
  sort_order: number
}

/** 采集草稿，对应 CrawlDraftOut */
export interface CrawlDraft {
  id: string
  source_platform: string
  source_note_id: string
  source_url: string
  raw_title: string | null
  raw_content: string
  author_name: string | null
  extracted_prompt_text: string | null
  extracted_category: string | null
  extracted_models: string | null
  status: 'pending' | 'approved' | 'rejected'
  approved_prompt_id: string | null
  review_note: string | null
  created_at: string
  reviewed_at: string | null
  images: CrawlDraftImage[]
}

/** 通用分页响应 */
export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** 采集任务，对应 CrawlTaskOut */
export interface CrawlTask {
  id: string
  batch_id: string
  source_platform: string
  keyword: string
  limit_count: number
  status: 'pending' | 'running' | 'awaiting_login' | 'reviewing' | 'success' | 'failed'
  new_draft_count: number
  schedule_id: string | null
  scheduled_for: string | null
  trigger_type: string
  published_count: number
  held_count: number
  stage: string | null
  log_tail: string | null
  error_message: string | null
  created_at: string
  finished_at: string | null
}

/** 可选采集平台，对应 CrawlPlatformInfo */
export interface CrawlPlatformInfo {
  value: string
  label: string
  warning: string | null
}

/** 分类枚举，与后端 app/routers/categories.py 的 CATEGORY_LABELS 保持一致。
 *  放在前端本地而不是每次请求接口获取，是为了让表单下拉框立刻可用、少一次网络往返。
 *  后端分类有变动时这里要同步修改。
 *  2026-10-09 改为按"创作意图"分类的八分类体系，原七分类已废弃。 */
export const CATEGORY_LABELS: Record<string, string> = {
  portrait: '人像写真',
  avatar: '头像 & IP',
  poster: '海报 & 封面',
  illustration: '插画 & 概念艺术',
  photography: '模拟摄影',
  character: '角色 & 人物设计',
  image_edit: '图片编辑',
  workflow: '工作流 & 技巧',
}

/** 采集草稿状态的中文标签 */
export const DRAFT_STATUS_LABELS: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已忽略',
}

/** 来源平台的中文标签（展示用，和后端 PLATFORM_LABELS 保持一致） */
export const PLATFORM_LABELS: Record<string, string> = {
  xiaohongshu: '小红书',
  weibo: '微博',
  kuaishou: '快手',
}

/** 采集任务状态的中文标签 */
export const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '采集中',
  awaiting_login: '等待扫码',
  reviewing: 'AI 审核中',
  success: '已完成',
  failed: '失败',
}

export interface CrawlSchedule {
  id: string
  name: string
  platforms: string[]
  keyword: string
  times: string[]
  limit_count: number
  enabled: boolean
  auto_publish: boolean
  next_run_at: string | null
  created_at: string
  updated_at: string
}
export type ScheduleInput = Omit<CrawlSchedule, 'id' | 'next_run_at' | 'created_at' | 'updated_at'>
export interface CrawlRunSummary {
  total: number
  success: number
  failed: number
  running: number
  new_drafts: number
  published: number
  held: number
}
export interface TaskLoginState {
  required: boolean
  status: string
  qr_available: boolean
  qr_version: string | number | null
  expires_at: string | null
  message: string
}
