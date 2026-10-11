import { beijingDate as bjDate, beijingTime as bjTime } from '../api/crawl-utils'
import { useCallback, useEffect, useState } from 'react'
import { listCrawlRuns, listSchedules, runSchedule, toggleSchedule } from '../api/admin'
import { ApiError } from '../api/client'
import { PLATFORM_LABELS, TASK_STATUS_LABELS } from '../api/types'
import type { CrawlSchedule, CrawlTask, CrawlRunSummary, Paginated } from '../api/types'
import { useAuth } from '../store/auth'
import ScheduleEditor from '../components/ScheduleEditor'
import TaskLoginDialog from '../components/TaskLoginDialog'

export default function CrawlSchedulesPage() {
  const { handleUnauthorized } = useAuth()
  const [schedules, setSchedules] = useState<CrawlSchedule[]>([])
  const [runs, setRuns] = useState<(Paginated<CrawlTask> & { summary: CrawlRunSummary }) | null>(null)
  const [date, setDate] = useState(bjDate)
  const [status, setStatus] = useState('')
  const [scheduleId, setScheduleId] = useState('')
  const [page, setPage] = useState(1)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<CrawlSchedule | null | undefined>(undefined)
  const [detail, setDetail] = useState<CrawlTask | null>(null)
  const [login, setLogin] = useState<string | null>(null)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const load = useCallback(async () => {
    try {
      const [items, history] = await Promise.all([listSchedules(), listCrawlRuns({ date, status, schedule_id: scheduleId, page })])
      setSchedules(items); setRuns(history); setError('')
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) handleUnauthorized()
      else setError(e instanceof Error ? e.message : '加载失败')
    }
  }, [date, status, scheduleId, page, handleUnauthorized])
  useEffect(() => { const initial = setTimeout(load, 0); const timer = setInterval(load, 5000); return () => { clearTimeout(initial); clearInterval(timer) } }, [load])
  async function action(id: string, run: boolean, enabled?: boolean) {
    setBusy(id); setNotice('')
    try { if (run) { await runSchedule(id); setDate(bjDate()); setPage(1); setNotice('任务已加入采集队列，可在下方查看进度。') } else await toggleSchedule(id, Boolean(enabled)); await load() }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败') }
    finally { setBusy('') }
  }
  const summary = runs?.summary
  return <>
    <div className="page-header"><div><h1>定时任务</h1><div className="subtitle">按北京时间自动采集、AI 审核与入库 · 每 5 秒更新执行状态</div></div><button className="btn btn-primary" onClick={() => setEditing(null)}>＋ 新建任务</button></div>
    {error && <div className="alert alert-error">{error}</div>}{notice && <div className="alert alert-success">{notice}</div>}
    <div className="card" style={{ marginBottom: 20 }}><div className="card-header">采集计划</div><div className="table-scroll"><table><thead><tr><th>名称 / 关键词</th><th>平台</th><th>每天时间</th><th>数量</th><th>发布方式</th><th>状态 / 下次执行</th><th>操作</th></tr></thead><tbody>
      {schedules.map((s) => <tr key={s.id}><td>{s.name}<div className="cell-muted">{s.keyword}</div></td><td>{s.platforms.map((p) => PLATFORM_LABELS[p] ?? p).join('、')}</td><td>{s.times.join('、')}</td><td>{s.limit_count} / 平台</td><td>{s.auto_publish ? 'AI 通过后发布' : '人工审核'}</td><td><span className={`tag ${s.enabled ? 'tag-success' : ''}`}>{s.enabled ? '已启用' : '已暂停'}</span><div className="cell-muted">{bjTime(s.next_run_at)}</div></td><td><div className="cell-actions"><button className="btn btn-sm" onClick={() => setEditing(s)}>编辑</button><button className="btn btn-sm" disabled={busy === s.id} onClick={() => action(s.id, false, !s.enabled)}>{s.enabled ? '暂停' : '启用'}</button><button className="btn btn-sm btn-primary" disabled={busy === s.id} onClick={() => action(s.id, true)}>立即执行</button></div></td></tr>)}
      {!schedules.length && <tr><td colSpan={7} className="empty">暂无定时任务，点击右上角新建。</td></tr>}
    </tbody></table></div></div>
    <div className="run-summary">{[['执行', summary?.total], ['成功', summary?.success], ['失败', summary?.failed], ['进行中', summary?.running], ['新增草稿', summary?.new_drafts], ['已发布', summary?.published], ['待人工处理', summary?.held]].map(([label, value]) => <div className="card" key={label}><span className="cell-muted">{label}</span><strong>{value ?? 0}</strong></div>)}</div>
    <div className="card"><div className="card-header run-filters"><span>每日执行记录</span><label>日期 <input type="date" value={date} onChange={(e) => { setDate(e.target.value); setPage(1) }} /></label><select aria-label="执行状态" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}><option value="">全部状态</option>{Object.entries(TASK_STATUS_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select><select aria-label="采集计划" value={scheduleId} onChange={(e) => { setScheduleId(e.target.value); setPage(1) }}><option value="">全部计划</option>{schedules.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select><button className="btn btn-sm" onClick={load}>刷新</button></div>
      <div className="table-scroll"><table><thead><tr><th>开始 / 结束</th><th>来源 / 平台</th><th>关键词 / 数量</th><th>状态 / 原因</th><th>结果</th><th>操作</th></tr></thead><tbody>
        {runs?.items.map((r) => <tr key={r.id}><td>{bjTime(r.created_at)}<div className="cell-muted">结束 {bjTime(r.finished_at)}</div></td><td>{r.trigger_type === 'scheduled' ? '定时执行' : '手动执行'}<div className="cell-muted">{PLATFORM_LABELS[r.source_platform] ?? r.source_platform}</div></td><td>{r.keyword}<div className="cell-muted">最多 {r.limit_count} 条</div></td><td><span className={`tag ${r.status === 'failed' ? 'tag-danger' : r.status === 'success' ? 'tag-success' : 'tag-primary'}`}>{TASK_STATUS_LABELS[r.status] ?? r.status}</span><div className="run-reason">{r.error_message || r.stage || (r.status === 'success' ? '采集审核完成' : '等待执行')}</div></td><td>新增 {r.new_draft_count}<div className="cell-muted">发布 {r.published_count ?? 0} · 保留 {r.held_count ?? 0}</div></td><td><div className="cell-actions"><button className="btn btn-sm" onClick={() => setDetail(r)}>详情 / 日志</button>{r.status === 'awaiting_login' && <button className="btn btn-sm btn-primary" onClick={() => setLogin(r.id)}>扫码登录</button>}</div></td></tr>)}
        {!runs?.items.length && <tr><td colSpan={6} className="empty">当天暂无执行记录</td></tr>}
      </tbody></table></div>
      <div className="pagination"><span>共 {runs?.total ?? 0} 条 · 第 {page} 页</span><button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><button className="btn btn-sm" disabled={page * 20 >= (runs?.total ?? 0)} onClick={() => setPage(page + 1)}>下一页</button></div>
    </div>
    {editing !== undefined && <ScheduleEditor schedule={editing} onClose={() => setEditing(undefined)} onSaved={() => { setEditing(undefined); void load() }} />}
    {login && <TaskLoginDialog taskId={login} onClose={() => { setLogin(null); void load() }} />}
    {detail && <div className="modal-mask"><section className="modal" role="dialog" aria-modal="true" aria-label="执行日志"><div className="modal-header">执行详情<button className="btn btn-sm" onClick={() => setDetail(null)}>关闭</button></div><div className="modal-body"><p>{detail.keyword} · {TASK_STATUS_LABELS[detail.status]}</p>{detail.error_message && <div className="alert alert-error">{detail.error_message}</div>}<pre className="raw-content" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{detail.log_tail || '暂无日志'}</pre></div></section></div>}
  </>
}
