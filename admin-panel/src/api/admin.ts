/**
 * 后台接口调用封装，按业务域分组。
 *
 * 后端路由定义见 app/routers/admin.py，所有接口都需要 X-Admin-Key
 * （由 client.ts 的 apiRequest 统一注入）。
 */

import { apiRequest } from './client'
import type { AdminPrompt, AdminReport, CrawlDraft, CrawlPlatformInfo, CrawlTask, Paginated } from './types'

// ---------- 内容管理 ----------

/** 请求体：创建内容时的一张效果图 */
export interface PromptImageInput {
  image_url: string
  thumbnail_url?: string | null
  sort_order?: number
}

/** 请求体：创建内容 */
export interface PromptCreateInput {
  title: string
  prompt_text: string
  description?: string | null
  category: string
  models?: string[]
  author_name?: string | null
  source_url?: string | null
  is_published?: boolean
  images?: PromptImageInput[]
}

/** 请求体：编辑内容（所有字段可选，PATCH 语义） */
export type PromptUpdateInput = Partial<PromptCreateInput>

/** 拉取内容列表，is_published 不传则返回全部（含草稿） */
export function listPrompts(params: {
  is_published?: boolean
  category?: string
  page?: number
  page_size?: number
}): Promise<Paginated<AdminPrompt>> {
  const query = new URLSearchParams()
  if (params.is_published !== undefined) {
    query.set('is_published', String(params.is_published))
  }
  if (params.category) query.set('category', params.category)
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  return apiRequest<Paginated<AdminPrompt>>(`/api/admin/prompts?${query}`)
}

/** 拉取单条内容详情 */
export function getPrompt(id: string): Promise<AdminPrompt> {
  return apiRequest<AdminPrompt>(`/api/admin/prompts/${id}`)
}

/** 创建内容（默认是草稿态） */
export function createPrompt(body: PromptCreateInput): Promise<AdminPrompt> {
  return apiRequest<AdminPrompt>('/api/admin/prompts', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 编辑内容 */
export function updatePrompt(id: string, body: PromptUpdateInput): Promise<AdminPrompt> {
  return apiRequest<AdminPrompt>(`/api/admin/prompts/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** 发布内容 */
export function publishPrompt(id: string): Promise<AdminPrompt> {
  return apiRequest<AdminPrompt>(`/api/admin/prompts/${id}/publish`, { method: 'POST' })
}

/** 下架内容 */
export function unpublishPrompt(id: string): Promise<AdminPrompt> {
  return apiRequest<AdminPrompt>(`/api/admin/prompts/${id}/unpublish`, { method: 'POST' })
}

/** 删除内容（不可恢复） */
export function deletePrompt(id: string): Promise<void> {
  return apiRequest<void>(`/api/admin/prompts/${id}`, { method: 'DELETE' })
}

// ---------- 举报管理 ----------

/** 拉取举报列表，status 不传则返回全部 */
export function listReports(params: {
  status?: string
  category?: string
  page?: number
  page_size?: number
}): Promise<Paginated<AdminReport>> {
  const query = new URLSearchParams()
  if (params.status) query.set('status', params.status)
  if (params.category) query.set('category', params.category)
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  return apiRequest<Paginated<AdminReport>>(`/api/admin/reports?${query}`)
}

/** 处理一条举报；unpublish_prompt=true 时联动下架被举报的内容 */
export function handleReport(
  id: string,
  body: { handled_note: string; unpublish_prompt?: boolean },
): Promise<AdminReport> {
  return apiRequest<AdminReport>(`/api/admin/reports/${id}/handle`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

// ---------- 爬虫采集草稿 ----------

/** 拉取采集草稿列表，常用 status=pending 筛出待审核的 */
export function listCrawlDrafts(params: {
  status?: string
  category?: string
  page?: number
  page_size?: number
}): Promise<Paginated<CrawlDraft>> {
  const query = new URLSearchParams()
  if (params.status) query.set('status', params.status)
  if (params.category) query.set('category', params.category)
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  return apiRequest<Paginated<CrawlDraft>>(`/api/admin/crawl-drafts?${query}`)
}

/** 拉取单条草稿详情（含正文全文和所有配图） */
export function getCrawlDraft(id: string): Promise<CrawlDraft> {
  return apiRequest<CrawlDraft>(`/api/admin/crawl-drafts/${id}`)
}

/** 审核时修正草稿字段（整理提示词原文/分类/模型），不会创建正式内容 */
export function updateCrawlDraft(
  id: string,
  body: {
    extracted_prompt_text?: string | null
    extracted_category?: string | null
    extracted_models?: string | null
  },
): Promise<CrawlDraft> {
  return apiRequest<CrawlDraft>(`/api/admin/crawl-drafts/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** 审核通过，草稿转为正式内容（默认仍是草稿态，需再调发布接口） */
export function approveCrawlDraft(
  id: string,
  body: {
    title: string
    prompt_text?: string | null
    category?: string | null
    models?: string[] | null
    is_published?: boolean
  },
): Promise<AdminPrompt> {
  return apiRequest<AdminPrompt>(`/api/admin/crawl-drafts/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 忽略一条草稿 */
export function rejectCrawlDraft(id: string, note?: string): Promise<CrawlDraft> {
  return apiRequest<CrawlDraft>(`/api/admin/crawl-drafts/${id}/reject`, {
    method: 'POST',
    body: JSON.stringify({ note: note ?? null }),
  })
}

// ---------- 采集任务（点击按钮触发 MediaCrawler，支持多平台） ----------

/** 获取当前支持的采集平台列表（含展示标签和已知内容局限提示） */
export function listCrawlPlatforms(): Promise<{ platforms: CrawlPlatformInfo[] }> {
  return apiRequest<{ platforms: CrawlPlatformInfo[] }>('/api/admin/crawl-platforms')
}

/** 开始一批采集任务：按勾选的平台分别创建一条任务（排队执行），返回全部任务记录 */
export function startCrawlTask(body: {
  platforms: string[]
  keyword: string
  limit: number
}): Promise<CrawlTask[]> {
  return apiRequest<CrawlTask[]>('/api/admin/crawl-tasks', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 查询单个采集任务当前状态 */
export function getCrawlTask(id: string): Promise<CrawlTask> {
  return apiRequest<CrawlTask>(`/api/admin/crawl-tasks/${id}`)
}

/** 采集任务历史列表 */
export function listCrawlTasks(params: { page?: number; page_size?: number }): Promise<Paginated<CrawlTask>> {
  const query = new URLSearchParams()
  query.set('page', String(params.page ?? 1))
  query.set('page_size', String(params.page_size ?? 20))
  return apiRequest<Paginated<CrawlTask>>(`/api/admin/crawl-tasks?${query}`)
}

// ---------- 定时任务与登录 ----------
export function listSchedules() {
  return apiRequest<import('./types').CrawlSchedule[]>('/api/admin/crawl-schedules')
}
export function saveSchedule(body: import('./types').ScheduleInput, id?: string) {
  return apiRequest<import('./types').CrawlSchedule>(`/api/admin/crawl-schedules${id ? '/' + id : ''}`, { method: id ? 'PATCH' : 'POST', body: JSON.stringify(body) })
}
export function toggleSchedule(id: string, enabled: boolean) {
  return apiRequest<import('./types').CrawlSchedule>(`/api/admin/crawl-schedules/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) })
}
export function runSchedule(id: string) {
  return apiRequest<CrawlTask[]>(`/api/admin/crawl-schedules/${id}/run`, { method: 'POST' })
}
export function listCrawlRuns(params: { date: string; status: string; schedule_id: string; page: number }) {
  const query = new URLSearchParams({ date: params.date, page: String(params.page), page_size: '20' })
  if (params.status) query.set('status', params.status)
  if (params.schedule_id) query.set('schedule_id', params.schedule_id)
  return apiRequest<Paginated<CrawlTask> & { summary: import('./types').CrawlRunSummary }>(`/api/admin/crawl-runs?${query}`)
}
export function getTaskLogin(id: string) {
  return apiRequest<import('./types').TaskLoginState>(`/api/admin/crawl-tasks/${id}/login`)
}
export function refreshTaskLogin(id: string) {
  return apiRequest<import('./types').TaskLoginState>(`/api/admin/crawl-tasks/${id}/login/refresh`, { method: 'POST' })
}
