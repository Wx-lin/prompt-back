/** 服务端时间必须按北京时间展示，未带时区的数据库时间按 UTC 解释。 */
export function beijingDate(now = new Date()): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now)
}
export function beijingTime(value: string | null): string {
  if (!value) return '—'
  const zoned = /Z$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`
  const time = new Date(zoned)
  if (Number.isNaN(time.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(time)
}
export function isActiveTask(status: string): boolean {
  return ['pending', 'running', 'awaiting_login', 'reviewing'].includes(status)
}
export function validateSchedule(input: { name: string; keyword: string; platforms: string[]; times: string[]; limit_count: number }): string {
  if (!input.name.trim() || !input.keyword.trim()) return '请填写任务名称和关键词'
  if (!input.platforms.length) return '请选择至少一个采集平台'
  if (!Number.isInteger(input.limit_count) || input.limit_count < 1 || input.limit_count > 50) return '每平台采集数量必须为 1–50 的整数'
  if (!input.times.length || input.times.some((t) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(t))) return '请填写有效的执行时间'
  if (new Set(input.times).size !== input.times.length) return '执行时间不能重复'
  return ''
}

export function hasNewQrVersion(available: boolean, next: string | number | null, previous: string | number | null | undefined): boolean {
  return available && next !== previous
}
export function hasLoginResumed(sawAwaitingLogin: boolean, required: boolean, status: string): boolean {
  return sawAwaitingLogin && !required && ['running', 'reviewing', 'success'].includes(status)
}
