import { useEffect, useState } from 'react'
import { parseScheduleKeywords, validateSchedule } from '../api/crawl-utils'
import { listCrawlPlatforms, saveSchedule } from '../api/admin'
import type { CrawlPlatformInfo, CrawlSchedule, ScheduleInput } from '../api/types'

const DEFAULT_KEYWORDS = ['AI头像提示词', '短剧人物提示词', 'AI人物写真提示词', '国风人物提示词', '电影感人像提示词', '电商产品图提示词', 'AI海报设计提示词', '室内设计提示词', 'AI场景概念图提示词', '动漫角色提示词', 'AI美食摄影提示词', 'Midjourney提示词']

export default function ScheduleEditor({ schedule, onClose, onSaved }: { schedule: CrawlSchedule | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<ScheduleInput>(schedule ?? { name: '', platforms: ['xiaohongshu'], keyword: 'AI头像提示词', times: ['10:00', '22:00'], limit_count: 20, enabled: true, auto_publish: true })
  const [keywordText, setKeywordText] = useState(() => (schedule ? (schedule.keywords?.length ? schedule.keywords : [schedule.keyword]) : DEFAULT_KEYWORDS).join('\n'))
  const [platforms, setPlatforms] = useState<CrawlPlatformInfo[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => { listCrawlPlatforms().then((r) => setPlatforms(r.platforms)).catch((e) => setError(e.message)) }, [])
  async function submit(e: React.FormEvent) {
    e.preventDefault()
    const keywords = parseScheduleKeywords(keywordText)
    const payload = { ...form, keywords, keyword: keywords[0] ?? '' }
    const validation = validateSchedule(payload)
    if (validation) { setError(validation); return }
    setSaving(true); setError('')
    try { await saveSchedule({ ...payload, name: form.name.trim(), times: [...form.times].sort() }, schedule?.id); onSaved() }
    catch (e) { setError(e instanceof Error ? e.message : '保存失败') }
    finally { setSaving(false) }
  }
  return <div className="modal-mask"><form className="modal" role="dialog" aria-modal="true" aria-labelledby="schedule-title" onSubmit={submit}>
    <div className="modal-header" id="schedule-title">{schedule ? '编辑定时任务' : '新建定时任务'}</div>
    <div className="modal-body">
      {error && <div className="alert alert-error">{error}</div>}
      <div className="field"><label htmlFor="schedule-name">任务名称</label><input id="schedule-name" required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
      <div className="field"><label>采集平台</label><div className="schedule-platforms">{platforms.map((p) => <label key={p.value}><input type="checkbox" checked={form.platforms.includes(p.value)} onChange={() => setForm({ ...form, platforms: form.platforms.includes(p.value) ? form.platforms.filter((x) => x !== p.value) : [...form.platforms, p.value] })} />{p.label}</label>)}</div></div>
      <div className="field"><label htmlFor="schedule-keyword">关键词列表（每行一个，最多 30 个）</label><textarea id="schedule-keyword" required rows={6} value={keywordText} onChange={(e) => setKeywordText(e.target.value)} /><p className="field-hint">每轮随机选择一个关键词；有多个关键词时避免连续重复。当前 {parseScheduleKeywords(keywordText).length} 个，保存时自动去重。每行一个，请勿使用英文逗号。</p></div>
      <div className="field"><label htmlFor="schedule-limit">每个平台每轮数量（1–50）</label><input id="schedule-limit" type="number" required min={1} max={50} value={form.limit_count} onChange={(e) => setForm({ ...form, limit_count: Number(e.target.value) })} /><p className="field-hint">小红书按去重后的新增数量继续翻页；达到翻页上限或连续多页无新增时结束，实际新增可能不足目标。</p></div>
      <div className="field"><label>每天执行时间（北京时间）</label><div className="schedule-times">{form.times.map((time, i) => <div key={i}><input type="time" required value={time} onChange={(e) => setForm({ ...form, times: form.times.map((t, n) => n === i ? e.target.value : t) })} /><button className="btn btn-sm" type="button" disabled={form.times.length === 1} onClick={() => setForm({ ...form, times: form.times.filter((_, n) => n !== i) })}>移除</button></div>)}</div><button className="btn btn-sm" type="button" style={{ marginTop: 8 }} onClick={() => setForm({ ...form, times: [...form.times, '12:00'] })}>＋ 添加时间</button></div>
      <div className="schedule-platforms"><label><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />启用任务</label><label><input type="checkbox" checked={form.auto_publish} onChange={(e) => setForm({ ...form, auto_publish: e.target.checked })} />AI 审核通过后自动发布</label></div>
      <p className="field-hint">审核未通过的内容保留供人工处理。平台登录过期时，可在执行记录中扫码。</p>
    </div>
    <div className="modal-footer"><button className="btn" type="button" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={saving || !platforms.length}>{saving ? '保存中…' : '保存任务'}</button></div>
  </form></div>
}
