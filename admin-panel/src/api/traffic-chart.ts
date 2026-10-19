export interface DailyVisitors { date: string; uv: number }
export function trackingDay(start: string | null): string | null {
  if (!start || !Number.isFinite(new Date(start).getTime())) return null
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(start))
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find(p => p.type === type)?.value
  return `${get('year')}-${get('month')}-${get('day')}`
}
export function trafficChartOption(daily: DailyVisitors[], days: 7 | 30, since: string | null) {
  const rows = daily.slice(-days)
  const first = trackingDay(since)
  return {
    animationDuration: 350,
    tooltip: { trigger: 'axis' as const, renderMode: 'richText' as const, axisPointer: { type: 'shadow' as const }, valueFormatter: (value: unknown) => value == null ? '尚未开始统计' : `${value} 位访客` },
    grid: { top: 18, left: 46, right: 16, bottom: 36 },
    xAxis: { type: 'category' as const, data: rows.map(row => row.date), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#64748b', fontSize: 11, interval: days === 7 ? 0 : 4, formatter: (value: string) => value.slice(5) } },
    yAxis: { type: 'value' as const, minInterval: 1, axisLabel: { color: '#64748b', fontSize: 11 }, splitLine: { lineStyle: { type: 'dashed' as const, color: '#edf1f7' } } },
    series: [{ name: '独立访客 UV', type: 'bar' as const, barMaxWidth: 26, itemStyle: { color: '#6366f1', borderRadius: [5, 5, 0, 0] }, data: rows.map(row => first && row.date >= first ? row.uv : null) }],
  }
}
