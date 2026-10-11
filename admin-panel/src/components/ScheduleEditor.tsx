import { useEffect, useState } from 'react'
import { validateSchedule } from '../api/crawl-utils'
import { listCrawlPlatforms, saveSchedule } from '../api/admin'
import type { CrawlPlatformInfo, CrawlSchedule, ScheduleInput } from '../api/types'

export default function ScheduleEditor({ schedule, onClose, onSaved }: { schedule: CrawlSchedule | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<ScheduleInput>(schedule ?? { name: '', platforms: ['xiaohongshu'], keyword: 'AI头像提示词', times: ['10:00', '22:00'], limit_count: 20, enabled: true, auto_publish: true })
  const [platforms, setPlatforms] = useState<CrawlPlatformInfo[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => { listCrawlPlatforms().then((r) => setPlatforms(r.platforms)).catch((e) => setError(e.message)) }, [])
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const validation = validateSchedule(form)
    if (validation) { setError(validation); return }
    setSaving(true); setError('')
    try { await saveSchedule({ ...form, name: form.name.trim(), keyword: form.keyword.trim(), times: [...form.times].sort() }, schedule?.id); onSaved() }
    catch (e) { setError(e instanceof Error ? e.message : '保存失败') }
    finally { setSaving(false) }
  }
  return <div className="modal-mask"><form className="modal" role="dialog" aria-modal="true" aria-labelledby="schedule-title" onSubmit={submit}>
    <div className="modal-header" id="schedule-title">{schedule ? '编辑定时任务' : '新建定时任务'}</div>
    <div className="modal-body">
      {error && <div className="alert alert-error">{error}</div>}
      <div className="field"><label htmlFor="schedule-name">任务名称</label><input id="schedule-name" required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
      <div className="field"><label>采集平台</label><div className="schedule-platforms">{platforms.map((p) => <label key={p.value}><input type="checkbox" checked={form.platforms.includes(p.value)} onChange={() => setForm({ ...form, platforms: form.platforms.includes(p.value) ? form.platforms.filter((x) => x !== p.value) : [...form.platforms, p.value] })} />{p.label}</label>)}</div></div>
      <div className="field"><label htmlFor="schedule-keyword">关键词</label><input id="schedule-keyword" required maxLength={200} value={form.keyword} onChange={(e) => setForm({ ...form, keyword: e.target.value })} /></div>
      <div className="field"><label htmlFor="schedule-limit">每个平台每轮数量（1–50）</label><input id="schedule-limit" type="number" required min={1} max={50} value={form.limit_count} onChange={(e) => setForm({ ...form, limit_count: Number(e.target.value) })} /></div>
      <div className="field"><label>每天执行时间（北京时间）</label><div className="schedule-times">{form.times.map((time, i) => <div key={i}><input type="time" required value={time} onChange={(e) => setForm({ ...form, times: form.times.map((t, n) => n === i ? e.target.value : t) })} /><button className="btn btn-sm" type="button" disabled={form.times.length === 1} onClick={() => setForm({ ...form, times: form.times.filter((_, n) => n !== i) })}>移除</button></div>)}</div><button className="btn btn-sm" type="button" style={{ marginTop: 8 }} onClick={() => setForm({ ...form, times: [...form.times, '12:00'] })}>＋ 添加时间</button></div>
      <div className="schedule-platforms"><label><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />启用任务</label><label><input type="checkbox" checked={form.auto_publish} onChange={(e) => setForm({ ...form, auto_publish: e.target.checked })} />AI 审核通过后自动发布</label></div>
      <p className="field-hint">审核未通过的内容保留供人工处理。平台登录过期时，可在执行记录中扫码。</p>
    </div>
    <div className="modal-footer"><button className="btn" type="button" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={saving || !platforms.length}>{saving ? '保存中…' : '保存任务'}</button></div>
  </form></div>
}
