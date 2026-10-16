import type { DashboardData } from './insights'

export const statusColors = ['#6366f1', '#f5b752', '#e98798', '#cbd5e1']
export function categoryChartOption(categories: DashboardData['categories']) {
  const sorted = categories.map((item, index) => ({ ...item, index })).sort((a, b) => b.total - a.total || a.index - b.index)
  return {
    animationDuration: 500,
    aria: { enabled: true },
    color: ['#c7d2fe', '#6366f1'],
    tooltip: { trigger: 'axis' as const, renderMode: 'richText' as const, backgroundColor: '#fff', borderColor: '#e2e8f0', textStyle: { color: '#334155', fontSize: 12 }, axisPointer: { type: 'shadow' as const, shadowStyle: { color: '#f1f5f980' } } },
    legend: { top: 0, right: 0, itemWidth: 9, itemHeight: 9, icon: 'roundRect', textStyle: { color: '#64748b', fontSize: 11 }, itemGap: 20 },
    grid: { top: 48, left: 44, right: 12, bottom: 54 },
    xAxis: { type: 'category' as const, data: sorted.map(item => item.label), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { interval: 0, color: '#64748b', fontSize: 10, margin: 18, formatter: (label: string) => label.length > 5 ? `${label.slice(0, 4)}\n${label.slice(4)}` : label } },
    yAxis: { type: 'value' as const, minInterval: 1, axisLabel: { color: '#94a3b8', fontSize: 11 }, splitLine: { lineStyle: { color: '#edf1f7', type: 'dashed' as const } } },
    series: [
      { name: '作品总量', type: 'bar' as const, data: sorted.map(item => item.total), barMaxWidth: 17, barGap: '22%', itemStyle: { borderRadius: [4, 4, 0, 0] } },
      { name: '已公开', type: 'bar' as const, data: sorted.map(item => item.published), barMaxWidth: 17, itemStyle: { borderRadius: [4, 4, 0, 0] } },
    ],
  }
}
export function statusChartOption(content: DashboardData['content']) {
  return {
    animationDuration: 500,
    aria: { enabled: true },
    color: statusColors,
    tooltip: { trigger: 'item' as const, renderMode: 'richText' as const, formatter: '{b}: {c} ({d}%)' },
    series: [{ type: 'pie' as const, radius: ['72%', '91%'], center: ['50%', '50%'], avoidLabelOverlap: true, stillShowZeroSum: false, showEmptyCircle: true, emptyCircleStyle: { color: '#f1f5f9' }, label: { show: false }, itemStyle: { borderRadius: 4, borderWidth: content.total ? 3 : 0, borderColor: '#fff' }, emphasis: { scaleSize: 4 }, data: [
      { name: '已公开', value: content.published }, { name: '待审核', value: content.pending }, { name: '已拒绝', value: content.rejected }, { name: '其他未公开', value: content.other_unpublished },
    ] }],
  }
}
