import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { getTraffic, type TrafficData } from '../api/traffic'
import { trafficChartOption } from '../api/traffic-chart'
import { formatBeijingTime } from '../api/insights-utils'
import { ApiError } from '../api/client'
import { useAuth } from '../store/auth'
const InsightChart = lazy(() => import('./InsightChart'))

export default function TrafficPanel() {
  const { handleUnauthorized } = useAuth()
  const [data, setData] = useState<TrafficData | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const [days, setDays] = useState<7 | 30>(7)
  useEffect(() => {
    const controller = new AbortController()
    getTraffic(controller.signal).then(result => { if (!controller.signal.aborted) setData(result) })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        if (err instanceof ApiError && err.status === 401) handleUnauthorized()
        else setError('访问统计暂时不可用，请重试。')
      }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [revision, handleUnauthorized])
  const chart = useMemo(() => trafficChartOption(data?.daily ?? [], days, data?.tracking_since ?? null), [data, days])
  function refresh() { setLoading(true); setError(''); setRevision(v => v + 1) }
  return <section className="insights-panel traffic-panel" aria-label="网站访问统计">
    <div className="insights-panel-heading"><div><div className="panel-eyebrow">WEBSITE TRAFFIC</div><h2>网站访问</h2><p>独立访客 UV · 未登录访问也计入</p></div><button className="btn btn-sm" disabled={loading} onClick={refresh}>{loading ? '更新中…' : '刷新访问数据'}</button></div>
    {error && <div className="alert alert-error" role="alert">{error}<button className="btn btn-sm" onClick={refresh}>重试访问统计</button></div>}
    {loading && !data && <div className="chart-loading" role="status">正在加载访问统计…</div>}
    {data && <>
      {error && <p className="insights-note">当前显示上次成功获取的数据。</p>}
      <div className="traffic-totals">{[['今日独立访客', data.today_uv], ['近7天独立访客', data.seven_day_uv], ['近30天独立访客', data.thirty_day_uv]].map(([label, value]) => <div className="traffic-total" key={label}><span>{label}</span><strong>{Number(value).toLocaleString()}</strong><small>按浏览器去重</small></div>)}</div>
      <div className="traffic-chart-title"><span>每日独立访客趋势</span><div className="traffic-range" role="group" aria-label="访问趋势时间范围"><button type="button" aria-pressed={days === 7} onClick={() => setDays(7)}>近7天</button><button type="button" aria-pressed={days === 30} onClick={() => setDays(30)}>近30天</button></div></div>
      <Suspense fallback={<div className="chart-loading">访问图表加载中…</div>}><InsightChart option={chart} label="每日独立访客柱状图" className="traffic-chart" /></Suspense>
      <div className="traffic-explanation"><span>{data.tracking_since ? `统计自 ${formatBeijingTime(data.tracking_since)}（北京时间）起，启用前无历史数据。` : '尚未开始记录访问数据。'}</span><span>近7/30天为区间去重，不是每日UV相加；跨设备、换浏览器或清除Cookie可能重复计数。</span></div>
    </>}
  </section>
}
