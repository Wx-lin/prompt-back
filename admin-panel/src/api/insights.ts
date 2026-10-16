import { apiRequest } from './client'
import { userSearchParams, type UserQuery } from './insights-utils'

export interface DashboardData {
  generated_at: string
  users: { total: number; enabled: number; disabled: number; today_new: number }
  content: { total: number; published: number; pending: number; rejected: number; other_unpublished: number; images: number }
  crawl: { total: number; pending: number }
  categories: { key: string; label: string; total: number; published: number }[]
}
export interface AdminUser {
  id: string
  nickname: string | null
  email: string | null
  is_active: boolean
  created_at: string | null
  last_login_at: string | null
}
export interface UserList {
  items: AdminUser[]
  total: number
  all_users_total: number
  page: number
  page_size: number
}
export function getDashboard(signal?: AbortSignal) {
  return apiRequest<DashboardData>('/api/admin/dashboard', { signal, cache: 'no-store' })
}
export function listUsers(query: UserQuery, signal?: AbortSignal) {
  return apiRequest<UserList>(`/api/admin/users?${userSearchParams(query)}`, { signal, cache: 'no-store' })
}
