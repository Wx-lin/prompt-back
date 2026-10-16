import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { ApiError } from '../api/client'
import { getSeo, importSeo, type SearchEngine, type SearchImport, type SeoData } from '../api/seo'
import { reportingWindow, createReadGate, parseSearchCsv, trendOption, type SearchDimension, type SearchRow } from '../api/seo-utils'
import { formatBeijingTime } from '../api/insights-utils'
import { useAuth } from '../store/auth'
import './insights.css'
import './seo.css'
const InsightChart = lazy(() => import('../components/InsightChart'))
const labels = {daily:'每日汇总',query:'关键词',page:'落地页'}
export default function SeoPage() {
  const { handleUnauthorized } = useAuth()
  const readGate = useMemo(() => createReadGate(), [])
  useEffect(() => () => readGate.cancel(), [readGate])
  const [engine,setEngine] = useState<SearchEngine>('google')
  const [start,setStart] = useState(() => reportingWindow('google').start); const [end,setEnd] = useState(() => reportingWindow('google').end)
  const [data,setData] = useState<SeoData|null>(null); const [error,setError] = useState(''); const [loading,setLoading] = useState(true)
  const [revision,setRevision] = useState(0); const [dimension,setDimension] = useState<SearchDimension>('daily')
  const [preview,setPreview] = useState<SearchImport|null>(null); const [busy,setBusy] = useState(false); const [notice,setNotice] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    // Query changes invalidate the previous report; never display another engine's data.
    // oxlint-disable-next-line react/set-state-in-effect
    readGate.cancel(); setLoading(true); setData(null); setError(''); setPreview(null)
    if(!start || !end || start > end) { setError('请选择有效的起止日期'); setLoading(false); return () => controller.abort() }
    getSeo(engine,start,end,controller.signal).then(value => {if(!controller.signal.aborted) setData(value)}).catch((err:unknown) => {
      if(controller.signal.aborted) return
      if(err instanceof ApiError && err.status === 401) handleUnauthorized()
      else setError(err instanceof Error ? err.message : '加载失败')
    }).finally(() => {if(!controller.signal.aborted) setLoading(false)})
    return () => controller.abort()
  },[engine,start,end,revision,handleUnauthorized,readGate])
  const chart = useMemo(() => trendOption(data?.daily ?? []),[data])
  async function loadFile(file:File) {
    const isCurrent = readGate.begin()
    setPreview(null); setError(''); setNotice('')
    try {
      if(file.size > 1024 * 1024) throw new Error('CSV 文件不能超过 1 MiB')
      const rows = parseSearchCsv(await file.text(),dimension)
      if(!isCurrent()) return
      setPreview({engine,dimension,start_date:start,end_date:end,rows})
    } catch(err) {if(isCurrent()) setError(err instanceof Error ? err.message : 'CSV 读取失败')}
  }
  async function save() {
    if(!preview) return
    setBusy(true);setError('')
    try {await importSeo(preview);setNotice('导入成功，相同来源、维度和日期范围的数据已替换');setPreview(null);setRevision(v=>v+1)}
    catch(err) {if(err instanceof ApiError && err.status === 401) handleUnauthorized();else setError(err instanceof Error ? err.message : '导入失败')}
    finally {setBusy(false)}
  }
  const totals = data?.totals
  return <div className="insights-page seo-page">
    <header className="insights-hero"><div className="insights-heading"><div className="page-eyebrow">WORKSPACE / SEARCH</div><h1>SEO 搜索表现</h1><p>aigc-prompt.com · 搜索引擎曝光与点击</p></div><button className="btn" disabled={loading||busy} onClick={()=>setRevision(v=>v+1)}>刷新数据</button></header>
    <div className="seo-filters"><label>搜索引擎<select aria-label="搜索引擎" disabled={busy} value={engine} onChange={e=>{readGate.cancel();setEngine(e.target.value as SearchEngine);setNotice('')}}><option value="google">Google</option><option value="baidu">百度</option></select></label><label>开始日期<input disabled={busy} type="date" value={start} onChange={e=>{readGate.cancel();setStart(e.target.value)}}/></label><label>结束日期<input disabled={busy} type="date" value={end} onChange={e=>{readGate.cancel();setEnd(e.target.value)}}/></label></div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}{notice && <p role="status">{notice}</p>}
    {loading && <p role="status">正在读取搜索数据…</p>}
    {data && <><p className="insights-note">{data.connection_status==='not_connected' ? '待接入：尚未导入该引擎的数据，不能据此判断没有搜索流量。' : `数据来源：人工导入 · 最近导入 ${formatBeijingTime(data.updated_at)}（北京时间）`}</p>
      <section className="insights-metrics" aria-label="搜索核心指标">{[['曝光次数',totals?.impressions.toLocaleString()],['点击次数',totals?.clicks.toLocaleString()],['点击率',totals ? `${(totals.ctr*100).toFixed(2)}%` : undefined],['平均排名',totals?.position?.toFixed(1)]].map(([label,value])=><div className="insights-metric" key={label}><div className="insights-metric-label">{label}</div><strong>{value??'—'}</strong><small>{data.coverage.daily.status==='complete' ? '所选范围内每日汇总' : '每日数据未完整覆盖所选范围'}</small></div>)}</section>
      <section className="insights-panel"><h2>搜索趋势</h2><p>日期按来源报表保留：{engine==='google'?'Google 太平洋时间':'百度原始报表时区'}。缺失日期不补零。</p>{data.daily.length ? <Suspense fallback={<p>图表加载中…</p>}><InsightChart option={chart} label="搜索点击与曝光趋势" className="seo-chart"/></Suspense> : <p>尚无每日汇总，请导入日期维度报表。</p>}</section>
      <ReportTable title="关键词表现" rows={data.queries} status={data.coverage.query.status}/><ReportTable title="落地页表现" rows={data.pages} status={data.coverage.page.status}/>
    </>}
    <section className="insights-panel seo-onboarding"><h2>接入搜索数据</h2><ol><li>在 <a href="https://search.google.com/search-console" target="_blank" rel="noreferrer">Google Search Console</a> 和 <a href="https://ziyuan.baidu.com/" target="_blank" rel="noreferrer">百度搜索资源平台</a> 添加并验证 aigc-prompt.com。验证账户由你自己登录。</li><li>提交 <a href="https://aigc-prompt.com/sitemap.xml" target="_blank" rel="noreferrer">sitemap.xml</a>。等待平台产生数据，再导出搜索表现 CSV。</li><li>选择与导出一致的引擎、日期范围和维度，上传后核对预览再导入。日期、关键词、页面分别导入，总量仅取日期报表。</li></ol><p>当前支持人工导入，尚未配置自动同步。只导入该域名的 Web 搜索报表，不混用图片、设备、国家筛选或不同时区的数据。关键词受平台隐私和导出限制，其合计可能少于总量。</p>
      <div className="seo-filters"><label>报表维度<select aria-label="报表维度" disabled={busy} value={dimension} onChange={e=>{readGate.cancel();setDimension(e.target.value as SearchDimension);setPreview(null)}}>{Object.entries(labels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><label>CSV 文件<input disabled={busy} type="file" accept=".csv,text/csv" onChange={e=>{const f=e.target.files?.[0];if(f) void loadFile(f);e.target.value=''}}/></label></div>
      {preview && <div><p>待导入：{preview.engine==='google'?'Google':'百度'} · {labels[preview.dimension]} · {preview.start_date} 至 {preview.end_date} · {preview.rows.length} 行。相同范围替换，交叉范围会拒绝。</p><ReportTable title="前五行预览" rows={preview.rows.slice(0,5)} status="preview"/><button className="btn btn-primary" disabled={busy} onClick={()=>void save()}>{busy?'导入中…':'确认导入'}</button></div>}
    </section><p className="insights-footnote">排名是来源平台报告的平均位置，不代表每次搜索的固定名次。本页不代表实时收录数量。</p>
  </div>
}
function ReportTable({title,rows,status}:{title:string;rows:SearchRow[];status:string}) {
 const [page,setPage] = useState(1)
 const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / 20)))
 const statuses:Record<string,string>={complete:'覆盖完整',partial:'部分日期数据',missing:'暂无数据',preview:'未保存'}
 return <section className="insights-panel seo-table"><h2>{title} <small>{statuses[status]}</small></h2>{rows.length ? <div className="insights-table-wrap"><table><thead><tr><th>内容</th><th>点击</th><th>曝光</th><th>点击率</th><th>平均排名</th></tr></thead><tbody>{rows.slice((currentPage-1)*20,currentPage*20).map((r,i)=><tr key={`${r.key}:${i}`}><td>{r.key}</td><td>{r.clicks.toLocaleString()}</td><td>{r.impressions.toLocaleString()}</td><td>{r.impressions ? `${(r.clicks/r.impressions*100).toFixed(2)}%`:'0.00%'}</td><td>{r.position?.toFixed(1)??'—'}</td></tr>)}</tbody></table><div className="seo-filters"><button className="btn" disabled={currentPage===1} onClick={()=>setPage(currentPage-1)}>上一页</button><span>第 {currentPage} / {Math.max(1,Math.ceil(rows.length/20))} 页 · {rows.length} 行</span><button className="btn" disabled={currentPage*20>=rows.length} onClick={()=>setPage(currentPage+1)}>下一页</button></div></div>:<p>所选范围尚无该维度报表，或已导入报表的日期范围与筛选不匹配。</p>}</section>
}
