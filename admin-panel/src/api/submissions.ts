import { apiRequest } from './client'
import type { Paginated } from './types'

export type ReviewStatus = 'pending' | 'approved' | 'rejected'
export interface Submission {
  id: string
  title: string
  prompt_text: string
  description: string | null
  category: string
  models: string[]
  author_name: string | null
  review_status: ReviewStatus
  published: boolean
  image_urls: string[]
  review_note: string | null
  created_at: string
}
export function listSubmissions(status: ReviewStatus, page: number, category = '') {
  return apiRequest<Paginated<Submission> & { has_more: boolean }>(
    `/api/admin/submissions?${new URLSearchParams({ status, page: String(page), page_size: '20', ...(category ? { category } : {}) })}`,
  )
}
export function reviewSubmission(id: string, action: 'approve' | 'reject', note: string, category?: string) {
  return apiRequest<Submission>(`/api/admin/submissions/${encodeURIComponent(id)}/${action}`, {
    method: 'POST', ...(action === 'reject' ? { body: JSON.stringify({ note: note.trim() }) } : { body: JSON.stringify({ category }) }),
  })
}
