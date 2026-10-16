export interface UserQuery {
  search?: string
  is_active?: boolean
  page: number
  page_size: number
}

export function userSearchParams(query: UserQuery): URLSearchParams {
  const params = new URLSearchParams({ page: String(query.page), page_size: String(query.page_size) })
  if (query.search?.trim()) params.set('search', query.search.trim())
  if (query.is_active !== undefined) params.set('is_active', String(query.is_active))
  return params
}

export function formatBeijingTime(value: string | null | undefined): string {
  if (!value) return '暂无'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '暂无'
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(date)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}:${part('second')}`
}
