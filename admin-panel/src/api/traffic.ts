import { apiRequest } from './client'
import type { DailyVisitors } from './traffic-chart'
export interface TrafficData {
  generated_at: string
  tracking_since: string | null
  timezone: 'Asia/Shanghai'
  today_uv: number
  seven_day_uv: number
  thirty_day_uv: number
  daily: DailyVisitors[]
}
export function getTraffic(signal?: AbortSignal) {
  return apiRequest<TrafficData>('/api/admin/traffic', { signal, cache: 'no-store' })
}
