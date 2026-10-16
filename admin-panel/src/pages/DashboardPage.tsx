import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { categoryChartOption, statusChartOption, statusColors } from '../api/insights-charts'
const InsightChart = lazy(() => import('../components/InsightChart'))
import { getDashboard, type DashboardData } from '../api/insights'
import { ApiError } from '../api/client'
import { formatBeijingTime } from '../api/insights-utils'
import { useAuth } from '../store/auth'
import './insights.css'

export default function DashboardPage() {
  const { handleUnauthorized } = useAuth()
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    // Request lifecycle resets when the query changes; cleanup aborts the previous request.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError('')
    getDashboard(controller.signal).then((result) => {
      if (!controller.signal.aborted) setData(result)
    }).catch((err: unknown) => {
      if (controller.signal.aborted) return
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else setError(err instanceof Error ? err.message : '加载失败，请重试')
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [revision, handleUnauthorized])

  const categoryOption = useMemo(() => categoryChartOption(data?.categories ?? []), [data])
  const statusOption = useMemo(() => data ? statusChartOption(data.content) : {}, [data])
  const statuses = data ? [ ['已公开', data.content.published], ['待审核', data.content.pending], ['已拒绝', data.content.rejected], ['其他未公开', data.content.other_unpublished] ] as const : []
  return <div className="insights-page">
    <header className="insights-hero"><div className="insights-heading"><div className="page-eyebrow">WORKSPACE / OVERVIEW</div><h1>数据大盘<span className="insights-live-dot">实时概览</span></h1><p>用户、内容与采集进度，一目了然。</p></div>
      <div className="insights-actions"><button className="btn insights-refresh" disabled={loading} onClick={() => setRevision(v => v + 1)}><span aria-hidden="true">↻</span> {loading ? '刷新中…' : '刷新数据'}</button>{data && <span className="insights-update">更新于 {formatBeijingTime(data.generated_at)} · 北京时间</span>}</div></header>
    {error && <div className="alert alert-error" role="alert">{error} <button className="btn btn-sm" onClick={() => setRevision(v => v + 1)}>重试</button></div>}
    {loading && !data && <div className="card empty" role="status">正在加载大盘数据…</div>}
    {data && <>
      {loading && <div className="insights-note" role="status">正在更新，当前显示上次数据</div>}
      <section className="insights-metrics" aria-label="核心指标">
        <Metric icon="users" label="用户总量" value={data.users.total} detail={`启用 ${data.users.enabled.toLocaleString()} · 停用 ${data.users.disabled.toLocaleString()}`} foot="全部注册账号" />
        <Metric icon="plus" label="今日新增用户" value={data.users.today_new} detail="北京时间今日 00:00 起" foot="今日注册" />
        <Metric icon="grid" label="作品总量" value={data.content.total} detail={`其中 ${data.content.published.toLocaleString()} 件已公开`} foot="内容资产" />
        <Metric icon="image" label="效果图总量" value={data.content.images} detail="按图片计数，一件作品可有多张" foot="图片资产" />
      </section>
      <div className="insights-charts-grid">
        <section className="insights-panel category-panel"><div className="insights-panel-heading"><div><div className="panel-eyebrow">CONTENT DISTRIBUTION</div><h2>分类库存</h2><p>各分类作品与公开数量对比</p></div><span className="insights-pill">{data.categories.filter(item => item.key !== '__other__').length} 个分类{data.categories.some(item => item.key === '__other__') ? ' + 历史' : ''}</span></div>
          <div className="category-chart-scroll"><Suspense fallback={<div className="chart-loading">图表加载中…</div>}><InsightChart option={categoryOption} label="分类库存柱状图，对比每个分类的作品总量与已公开数量" className="category-chart" /></Suspense></div>
          <div className="chart-caption"><span><i /> 以作品为单位，按库存量排序</span><span>全部分类 · 包含零库存</span></div>
        </section>
        <section className="insights-panel status-panel"><div className="insights-panel-heading"><div><div className="panel-eyebrow">PUBLISHING STATUS</div><h2>作品状态</h2><p>内容公开与审核分布</p></div></div>
          <div className="status-chart-wrap"><Suspense fallback={<div className="chart-loading">图表加载中…</div>}><InsightChart option={statusOption} label="作品状态环形图" className="status-chart" /></Suspense><div className="donut-center"><strong>{data.content.total ? Math.round(data.content.published / data.content.total * 100) : 0}<small>%</small></strong><span>{data.content.total ? '作品已公开' : '暂无作品'}</span></div></div>
          <div className="status-legend">{statuses.map(([label, count], index) => <div key={label}><span><i style={{ background: statusColors[index] }} />{label}</span><strong>{count.toLocaleString()}</strong></div>)}</div>
        </section>
      </div>
      <section className="insights-panel crawl-panel"><div className="crawl-symbol" aria-hidden="true">↓</div><div className="crawl-heading"><div className="panel-eyebrow">COLLECTION PIPELINE</div><h2>采集草稿</h2><p>独立于作品库，审核后再入库。</p></div><div className="crawl-number"><span>草稿总量</span><strong>{data.crawl.total.toLocaleString()}</strong></div><div className="crawl-number pending"><span><i />待处理</span><strong>{data.crawl.pending.toLocaleString()}</strong></div><div className="crawl-progress"><div><span style={{ width: `${data.crawl.total ? Math.min(100, data.crawl.pending / data.crawl.total * 100) : 0}%` }} /></div><span>{data.crawl.total ? Math.round(data.crawl.pending / data.crawl.total * 100) : 0}% 草稿待处理</span></div></section>
      <details className="insights-panel category-details"><summary>分类明细<span>查看完整数据 ↗</span></summary><div className="insights-table-wrap"><table><thead><tr><th>分类</th><th>作品总量</th><th>已公开</th></tr></thead><tbody>{data.categories.map(item => <tr key={item.key}><td>{item.label}</td><td>{item.total.toLocaleString()}</td><td>{item.published.toLocaleString()}</td></tr>)}</tbody></table></div></details>
      <p className="insights-footnote"><span>ⓘ</span> 数据包含测试账号；账号启用表示账号状态，不代表访问活跃度。各作品状态互不重复。</p>
    </>}
  </div>
}
function Metric({ label, value, detail, foot, icon }: { label: string; value: number; detail: string; foot: string; icon: string }) {
  const paths: Record<string, string> = { users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0', plus: 'M12 8v8M8 12h8M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0', grid: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z', image: 'M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM3 16l5-5 5 5 3-3 5 5M8 7h.01' }
  return <div className={`insights-metric metric-${icon}`}><div className="metric-top"><div className="insights-metric-label">{label}</div><span className="metric-icon"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={paths[icon]} /></svg></span></div><strong>{value.toLocaleString()}</strong><div className="metric-bottom"><span>{detail}</span><small>{foot}</small></div></div>
}
