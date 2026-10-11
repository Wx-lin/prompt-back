/**
 * 举报管理页：查看用户提交的举报，处理（备注 + 可选联动下架被举报内容）。
 */

import { useCallback, useEffect, useState } from 'react'
import dayjs from 'dayjs'
import { listReports, handleReport } from '../api/admin'
import type { AdminReport, Paginated } from '../api/types'
import { ApiError } from '../api/client'
import { useAuth } from '../store/auth'

const PAGE_SIZE = 20

const FILTER_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'resolved', label: '已处理' },
]

export default function ReportsPage() {
  const { handleUnauthorized } = useAuth()

  const [data, setData] = useState<Paginated<AdminReport> | null>(null)
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState('pending')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // 处理弹窗
  const [handling, setHandling] = useState<AdminReport | null>(null)

  const fetchData = useCallback(
    async (p: number, f: string) => {
      setLoading(true)
      setError('')
      try {
        const res = await listReports({ status: f || undefined, page: p, page_size: PAGE_SIZE })
        setData(res)
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) handleUnauthorized()
        else setError(err instanceof Error ? err.message : '加载失败')
      } finally {
        setLoading(false)
      }
    },
    [handleUnauthorized],
  )

  useEffect(() => {
    fetchData(page, filter)
  }, [fetchData, page, filter])

  function statusTag(status: string) {
    if (status === 'pending') return <span className="tag tag-warning">待处理</span>
    return <span className="tag tag-success">已处理</span>
  }

  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>举报管理</h1>
          {data && <div className="subtitle">共 {total} 条举报</div>}
        </div>
        <div className="toolbar">
          <select
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value)
              setPage(1)
            }}
            style={{ width: 'auto' }}
          >
            {FILTER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>被举报内容</th>
              <th>举报原因</th>
              <th>举报邮箱</th>
              <th>状态</th>
              <th>处理备注</th>
              <th>举报时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={7} className="empty spinner-inline">
                  加载中...
                </td>
              </tr>
            )}
            {!loading && data?.items.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">
                  暂无举报记录
                </td>
              </tr>
            )}
            {!loading &&
              data?.items.map((r) => (
                <tr key={r.id}>
                  <td className="cell-title" title={r.prompt_title ?? undefined}>
                    {r.prompt_title ?? <span className="cell-muted">（内容已删除）</span>}
                  </td>
                  <td style={{ maxWidth: 200, wordBreak: 'break-all' }}>{r.reason}</td>
                  <td className="cell-muted">{r.reporter_email ?? '—'}</td>
                  <td>{statusTag(r.status)}</td>
                  <td className="cell-muted" style={{ maxWidth: 160 }}>
                    {r.handled_note ?? '—'}
                  </td>
                  <td className="cell-muted">{dayjs(r.created_at).format('MM-DD HH:mm')}</td>
                  <td>
                    {r.status === 'pending' && (
                      <button className="btn btn-sm btn-primary" onClick={() => setHandling(r)}>
                        处理
                      </button>
                    )}
                    {r.status === 'resolved' && (
                      <span className="cell-muted">
                        {r.handled_at ? dayjs(r.handled_at).format('MM-DD HH:mm') : '—'}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>

        {totalPages > 1 && (
          <div className="pagination">
            <span>
              第 {page} / {totalPages} 页，共 {total} 条
            </span>
            <button className="btn btn-sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              上一页
            </button>
            <button
              className="btn btn-sm"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </button>
          </div>
        )}
      </div>

      {handling && (
        <HandleModal
          report={handling}
          onClose={() => setHandling(null)}
          onSaved={() => {
            setHandling(null)
            fetchData(page, filter)
          }}
          onUnauthorized={handleUnauthorized}
        />
      )}
    </>
  )
}

// ---------- 处理弹窗 ----------

interface HandleModalProps {
  report: AdminReport
  onClose: () => void
  onSaved: () => void
  onUnauthorized: () => void
}

function HandleModal({ report, onClose, onSaved, onUnauthorized }: HandleModalProps) {
  const [note, setNote] = useState('')
  const [unpublish, setUnpublish] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!note.trim()) {
      setError('请填写处理备注')
      return
    }
    setSaving(true)
    setError('')
    try {
      await handleReport(report.id, { handled_note: note.trim(), unpublish_prompt: unpublish })
      onSaved()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized()
      else setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-mask" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={{ maxWidth: 480 }}>
        <div className="modal-header">
          <span>处理举报</span>
          <button className="btn btn-sm" onClick={onClose}>
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            {error && <div className="alert alert-error">{error}</div>}

            <div className="field">
              <label>被举报内容</label>
              <div style={{ padding: '5px 0', fontWeight: 500 }}>
                {report.prompt_title ?? '（内容已删除）'}
              </div>
            </div>

            <div className="field">
              <label>举报原因</label>
              <div className="raw-content" style={{ maxHeight: 100 }}>
                {report.reason}
              </div>
            </div>

            <div className="field">
              <label>处理备注 *</label>
              <textarea
                required
                rows={3}
                placeholder="说明处理结果，例如：内容合规无需处理 / 已下架违规内容"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </div>

            <div className="field">
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--color-danger)' }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={unpublish}
                  onChange={(e) => setUnpublish(e.target.checked)}
                />
                同时下架被举报内容
              </label>
            </div>
          </div>

          <div className="modal-footer">
            <button type="button" className="btn" onClick={onClose}>
              取消
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? '处理中...' : '确认处理'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
