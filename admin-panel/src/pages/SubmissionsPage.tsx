import { useEffect, useRef, useState } from 'react'
import dayjs from 'dayjs'
import { ApiError } from '../api/client'
import { listSubmissions, reviewSubmission, type ReviewStatus, type Submission } from '../api/submissions'
import { CATEGORY_LABELS } from '../api/types'
import { useAuth } from '../store/auth'

const STATUS: Record<ReviewStatus, string> = { pending: '待审核', approved: '已通过', rejected: '已拒绝' }

export default function SubmissionsPage() {
  const { handleUnauthorized } = useAuth()
  const [status, setStatus] = useState<ReviewStatus>('pending')
  const [page, setPage] = useState(1)
  const [version, setVersion] = useState(0)
  const [data, setData] = useState<Awaited<ReturnType<typeof listSubmissions>> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const requestPending = useRef(false)

  useEffect(() => {
    let active = true
    listSubmissions(status, page).then(result => {
      if (!active) return
      if (page > 1 && result.items.length === 0) setPage(page - 1)
      else setData(result)
    }).catch(err => {
      if (!active) return
      setData(null)
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else setError(err instanceof Error ? err.message : '加载投稿失败')
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [status, page, version, handleUnauthorized])

  async function review(item: Submission, action: 'approve' | 'reject', note: string) {
    if (requestPending.current || item.review_status !== 'pending') return
    if (action === 'reject' && (!note.trim() || note.trim().length > 1000)) return
    requestPending.current = true
    setBusy(item.id)
    setError('')
    setNotice('')
    try {
      await reviewSubmission(item.id, action, note)
      setNotice(action === 'approve' ? '审核通过，作品已发布。' : '已拒绝投稿并保存原因。')
      setLoading(true)
      setVersion(value => value + 1)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else {
        setError(err instanceof Error ? err.message : '审核操作失败')
        if (err instanceof ApiError && err.status === 409) { setLoading(true); setVersion(value => value + 1) }
      }
    } finally {
      requestPending.current = false
      setBusy(null)
    }
  }

  return <>
    <div className="page-header">
      <div><h1>投稿审核</h1><div className="subtitle">用户投稿经审核通过后公开展示</div></div>
      <div className="toolbar">
        <select aria-label="审核状态" value={status} disabled={busy !== null} onChange={event => {
          setLoading(true); setStatus(event.target.value as ReviewStatus); setPage(1); setData(null); setError(''); setNotice('')
        }}>
          {Object.entries(STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <button className="btn" disabled={loading || busy !== null} onClick={() => { setLoading(true); setError(''); setVersion(value => value + 1) }}>刷新</button>
      </div>
    </div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {notice && <div className="alert" role="status">{notice}</div>}
    {loading ? <div className="empty" role="status">加载中...</div> : data?.items.length === 0 ? <div className="card empty">暂无{STATUS[status]}投稿</div> :
      data?.items.map(item => <SubmissionCard key={item.id} item={item} disabled={busy !== null}
        working={busy === item.id} onReview={(action, note) => review(item, action, note)} />)}
    {data && <div className="pagination">
      <span>共 {data.total} 条 · 第 {page} 页</span>
      <button className="btn btn-sm" disabled={loading || busy !== null || page <= 1} onClick={() => { setLoading(true); setPage(value => value - 1) }}>上一页</button>
      <button className="btn btn-sm" disabled={loading || busy !== null || !data.has_more} onClick={() => { setLoading(true); setPage(value => value + 1) }}>下一页</button>
    </div>}
  </>
}

function SubmissionCard({ item, disabled, working, onReview }: {
  item: Submission; disabled: boolean; working: boolean
  onReview: (action: 'approve' | 'reject', note: string) => Promise<void>
}) {
  const [note, setNote] = useState('')
  return <article className="card submission-card" aria-labelledby={`title-${item.id}`}>
    <h2 id={`title-${item.id}`}>{item.title}</h2>
    <p className="cell-muted">{item.author_name || '用户'} · {dayjs(item.created_at).format('YYYY-MM-DD HH:mm')} · {STATUS[item.review_status]}</p>
    <div className="submission-images">{item.image_urls.map((url, index) =>
      /^https?:\/\//i.test(url) && <a href={url} key={`${index}-${url}`} target="_blank" rel="noopener noreferrer">
        <img src={url} alt={`${item.title} 效果图 ${index + 1}`} loading="lazy" referrerPolicy="no-referrer" />
      </a>)}</div>
    <p>分类：{CATEGORY_LABELS[item.category] ?? item.category} · 模型：{item.models.join('、') || '未标注'}</p>
    <details><summary>查看完整提示词与说明</summary><p className="submission-text">{item.prompt_text}</p>
      {item.description && <p className="submission-text cell-muted">{item.description}</p>}</details>
    {item.review_note && <p className="submission-text">审核备注：{item.review_note}</p>}
    {item.review_status === 'pending' && <form onSubmit={event => { event.preventDefault(); void onReview('reject', note) }}>
      <div className="field">
        <label htmlFor={`note-${item.id}`}>拒绝原因</label>
        <textarea id={`note-${item.id}`} value={note} maxLength={1000} rows={2} disabled={disabled}
          onChange={event => setNote(event.target.value)} placeholder="拒绝时必填，最多 1000 字" />
      </div>
      <div className="cell-actions">
        <button type="button" className="btn btn-primary" disabled={disabled} onClick={() => void onReview('approve', '')}>
          {working ? '处理中...' : '通过并发布'}</button>
        <button type="submit" className="btn btn-danger" disabled={disabled || !note.trim()}>拒绝投稿</button>
      </div>
    </form>}
  </article>
}
