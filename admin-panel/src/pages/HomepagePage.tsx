import { useEffect, useRef, useState } from 'react'
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core'
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { ApiError } from '../api/client'
import { getCandidates, getFeatured, saveFeatured, type FeaturedPrompt } from '../api/homepage'
import { useAuth } from '../store/auth'

export default function HomepagePage() {
  const { handleUnauthorized } = useAuth()
  const [items, setItems] = useState<FeaturedPrompt[]>([])
  const [savedIds, setSavedIds] = useState<string[]>([])
  const [revision, setRevision] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [conflict, setConflict] = useState(false)
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [candidates, setCandidates] = useState<Awaited<ReturnType<typeof getCandidates>> | null>(null)
  const [candidateLoading, setCandidateLoading] = useState(true)
  const [preview, setPreview] = useState<'desktop' | 'mobile'>('desktop')
  const [candidateError, setCandidateError] = useState('')
  const [candidateVersion, setCandidateVersion] = useState(0)
  const pending = useRef(false)
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }))
  const dirty = JSON.stringify(items.map(item => item.id)) !== JSON.stringify(savedIds)

  function failure(err: unknown) {
    if (err instanceof ApiError && err.status === 401) handleUnauthorized()
    else setError(err instanceof Error ? err.message : '操作失败，请重试')
  }
  async function reload() {
    if (dirty && !window.confirm('重新加载会放弃未保存的排序，是否继续？')) return
    setLoading(true); setError('')
    try {
      const result = await getFeatured()
      setItems(result.items); setSavedIds(result.items.map(item => item.id)); setRevision(result.revision); setConflict(false); setNotice('')
    } catch (err) { failure(err) } finally { setLoading(false) }
  }
  useEffect(() => {
    let active = true
    getFeatured().then(result => {
      if (!active) return
      setItems(result.items); setSavedIds(result.items.map(item => item.id)); setRevision(result.revision)
      try {
        const draft = JSON.parse(sessionStorage.getItem('homepage_featured_draft') || 'null') as { items?: FeaturedPrompt[]; revision?: string } | null
        if (draft && Array.isArray(draft.items) && draft.items.length <= 50 && typeof draft.revision === 'string' && draft.items.every(item => typeof item.id === 'string' && typeof item.title === 'string') && window.confirm('发现本标签页未保存的推荐排序，是否恢复？')) {
          setItems(draft.items); setRevision(draft.revision)
          if (draft.revision !== result.revision) { setConflict(true); setError('未保存排序已恢复，但服务端推荐已更新。请重新加载最新内容后调整，避免覆盖他人的修改。') }
          else setNotice('已恢复本标签页未保存的排序，请检查后保存。')
        }
      } catch { /* 浏览器禁用存储或旧草稿无效时，以服务端结果为准。 */ }
    }).catch(err => {
      if (!active) return
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else setError(err instanceof Error ? err.message : '加载推荐失败')
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [handleUnauthorized])
  useEffect(() => {
    let active = true
    getCandidates(search, page).then(result => { if (active) setCandidates(result) }).catch(err => {
      if (!active) return
      setCandidates(null)
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else setCandidateError(err instanceof Error ? err.message : '加载候选作品失败')
    }).finally(() => { if (active) setCandidateLoading(false) })
    return () => { active = false }
  }, [search, page, candidateVersion, handleUnauthorized])
  useEffect(() => {
    if (loading || !revision) return
    try {
      if (dirty) sessionStorage.setItem('homepage_featured_draft', JSON.stringify({ items, revision }))
      else sessionStorage.removeItem('homepage_featured_draft')
    } catch { /* 存储不可用时，链接离开与刷新确认仍生效。 */ }
  }, [items, revision, dirty, loading])
  useEffect(() => {
    if (!dirty && !busy) return
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault() }
    const navigate = (event: MouseEvent) => {
      const link = (event.target as Element).closest('a')
      if (link && !window.confirm('排序尚未保存，离开会放弃更改。是否继续？')) { event.preventDefault(); event.stopPropagation() }
    }
    window.addEventListener('beforeunload', unload); document.addEventListener('click', navigate, true)
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true) }
  }, [dirty, busy])
  async function save() {
    if (pending.current || !revision || conflict) return
    pending.current = true; setBusy(true); setError(''); setNotice('')
    try {
      const result = await saveFeatured(items, revision)
      setItems(result.items); setSavedIds(result.items.map(item => item.id)); setRevision(result.revision); setNotice('推荐顺序已保存，首页将按此顺序展示。')
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) { setConflict(true); setError('推荐已被其他操作修改。你的更改仍保留，请点击重新加载确认最新内容后再调整。') }
      else failure(err)
    } finally { pending.current = false; setBusy(false) }
  }
  const disabled = loading || busy || !revision
  return <>
    <div className="page-header"><div><h1>首页推荐</h1><div className="subtitle">精选按顺序优先展示，之后衔接热门作品。最多 50 个；手机和桌面每屏数量不同。</div></div>
      <div className="toolbar"><button className="btn" disabled={busy || loading} onClick={() => void reload()}>重新加载</button>
        <button className="btn btn-primary" disabled={disabled || !dirty || conflict} onClick={() => void save()}>{busy ? '保存中...' : '保存推荐'}</button></div></div>
    {error && <div className="alert alert-error" role="alert">{error}</div>}{notice && <div className="alert" role="status">{notice}</div>}
    <section className="card homepage-section"><h2>推荐队列（{items.length}/50）{dirty ? ' · 未保存' : ''}</h2>
      {loading ? <p role="status">加载中...</p> : <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={({ active, over }) => {
        if (disabled || !over || active.id === over.id) return
        setItems(current => arrayMove(current, current.findIndex(item => item.id === active.id), current.findIndex(item => item.id === over.id)))
      }}><SortableContext items={items.map(item => item.id)} strategy={verticalListSortingStrategy}>
        {items.length === 0 && <p className="cell-muted">尚未设置精选，首页按热度排序。请从下方加入作品。</p>}
        {items.map((item, index) => <QueueRow key={item.id} item={item} index={index} total={items.length} disabled={disabled}
          onMove={offset => setItems(current => arrayMove(current, index, index + offset))} onRemove={() => setItems(current => current.filter(row => row.id !== item.id))} />)}
      </SortableContext></DndContext>}
    </section>
    <section className="card homepage-section"><h2>顺序预览</h2><div className="toolbar"><button className="btn btn-sm" aria-pressed={preview === 'desktop'} onClick={() => setPreview('desktop')}>桌面预览</button><button className="btn btn-sm" aria-pressed={preview === 'mobile'} onClick={() => setPreview('mobile')}>手机预览</button></div>
      <p className="cell-muted">显示队列前 24 个作品。此处用于检查顺序与题材搭配，实际每屏数量取决于屏幕和图片比例。</p>
      <div className={`homepage-preview homepage-preview-${preview}`}>{items.slice(0, 24).map((item, index) => <article key={item.id}><Thumbnail item={item} /><p>{index + 1}. {item.title}</p></article>)}</div>
    </section>
    <section className="card homepage-section"><h2>选择已公开作品</h2><form className="toolbar" onSubmit={event => { event.preventDefault(); if (search !== query.trim() || page !== 1) { setCandidateLoading(true); setCandidateError(''); setSearch(query.trim()); setPage(1) } }}>
      <input aria-label="搜索作品" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索标题或提示词" maxLength={200} /><button className="btn" type="submit">搜索</button></form>
      {candidateError && <div role="alert" className="alert alert-error">{candidateError} <button className="btn btn-sm" onClick={() => { setCandidateLoading(true); setCandidateError(''); setCandidateVersion(current => current + 1) }}>重试加载作品</button></div>}
      {candidateLoading ? <p role="status">加载作品中...</p> : <div className="homepage-candidates">{candidates?.items.map(item => {
        const added = items.some(row => row.id === item.id)
        return <article className="homepage-candidate" key={item.id}><Thumbnail item={item} /><span>{item.title}</span>
          <button className="btn btn-sm" disabled={disabled || added || items.length >= 50 || !validImage(item.thumbnail_url)} onClick={() => setItems(current => current.some(row => row.id === item.id) || current.length >= 50 ? current : [...current, item])}>{added ? '已加入' : '加入推荐'}</button></article>
      })}{candidates?.items.length === 0 && <p>没有匹配的已公开作品</p>}</div>}
      <div className="pagination"><span>共 {candidates?.total ?? 0} 个 · 第 {page} 页</span><button className="btn btn-sm" disabled={candidateLoading || page <= 1} onClick={() => { setCandidateLoading(true); setCandidateError(''); setPage(current => current - 1) }}>上一页</button><button className="btn btn-sm" disabled={candidateLoading || !candidates?.has_more} onClick={() => { setCandidateLoading(true); setCandidateError(''); setPage(current => current + 1) }}>下一页</button></div>
    </section>
  </>
}
function validImage(url: string | null) { return !!url && /^https?:\/\//i.test(url) }
function Thumbnail({ item }: { item: FeaturedPrompt }) { return validImage(item.thumbnail_url) ? <img className="homepage-thumbnail" src={item.thumbnail_url!} alt={item.title} loading="lazy" referrerPolicy="no-referrer" /> : <span className="homepage-thumbnail">缺少图片</span> }
function QueueRow({ item, index, total, disabled, onMove, onRemove }: { item: FeaturedPrompt; index: number; total: number; disabled: boolean; onMove: (offset: number) => void; onRemove: () => void }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: item.id, disabled })
  return <div ref={setNodeRef} className="homepage-row" style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.6 : 1 }}>
    <button className="btn btn-sm homepage-drag" disabled={disabled} {...attributes} {...listeners} aria-label={`拖拽排序 ${item.title}`}>拖动</button><span>{index + 1}</span><Thumbnail item={item} /><span className="homepage-title">{item.title}</span>
    <div className="cell-actions"><button className="btn btn-sm" disabled={disabled || index === 0} onClick={() => onMove(-1)} aria-label={`上移 ${item.title}`}>上移</button><button className="btn btn-sm" disabled={disabled || index === total - 1} onClick={() => onMove(1)} aria-label={`下移 ${item.title}`}>下移</button><button className="btn btn-sm btn-danger" disabled={disabled} onClick={onRemove} aria-label={`移除 ${item.title}`}>移除</button></div>
  </div>
}
