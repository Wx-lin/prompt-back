/**
 * 内容管理页：列表 + 新建/编辑弹窗 + 发布/下架/删除操作。
 */

import { useCallback, useEffect, useState } from 'react'
import dayjs from 'dayjs'
import {
  listPrompts,
  createPrompt,
  updatePrompt,
  publishPrompt,
  unpublishPrompt,
  deletePrompt,
  type PromptCreateInput,
} from '../api/admin'
import type { AdminPrompt, Paginated } from '../api/types'
import { CATEGORY_LABELS } from '../api/types'
import { ApiError } from '../api/client'
import { useAuth } from '../store/auth'

const PAGE_SIZE = 20

// 筛选选项
const FILTER_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'false', label: '草稿' },
  { value: 'true', label: '已发布' },
]

export default function PromptsPage() {
  const { handleUnauthorized } = useAuth()

  const [data, setData] = useState<Paginated<AdminPrompt> | null>(null)
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // 弹窗状态
  const [showModal, setShowModal] = useState(false)
  const [editTarget, setEditTarget] = useState<AdminPrompt | null>(null)

  const fetchData = useCallback(
    async (p: number, f: string) => {
      setLoading(true)
      setError('')
      try {
        const params: Parameters<typeof listPrompts>[0] = { page: p, page_size: PAGE_SIZE }
        if (f !== '') params.is_published = f === 'true'
        const res = await listPrompts(params)
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

  function openCreate() {
    setEditTarget(null)
    setShowModal(true)
  }

  function openEdit(p: AdminPrompt) {
    setEditTarget(p)
    setShowModal(true)
  }

  async function handlePublish(p: AdminPrompt) {
    try {
      await (p.is_published ? unpublishPrompt(p.id) : publishPrompt(p.id))
      fetchData(page, filter)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else alert(err instanceof Error ? err.message : '操作失败')
    }
  }

  async function handleDelete(p: AdminPrompt) {
    if (!confirm(`确认删除「${p.title}」？此操作不可恢复。`)) return
    try {
      await deletePrompt(p.id)
      fetchData(page, filter)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else alert(err instanceof Error ? err.message : '删除失败')
    }
  }

  function handleModalSaved() {
    setShowModal(false)
    fetchData(page, filter)
  }

  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>内容管理</h1>
          {data && <div className="subtitle">共 {total} 条内容</div>}
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
          <button className="btn btn-primary" onClick={openCreate}>
            + 新建内容
          </button>
        </div>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>标题</th>
              <th>分类</th>
              <th>模型</th>
              <th>状态</th>
              <th>浏览</th>
              <th>创建时间</th>
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
                  暂无内容
                </td>
              </tr>
            )}
            {!loading &&
              data?.items.map((p) => (
                <tr key={p.id}>
                  <td className="cell-title" title={p.title}>
                    {p.title}
                  </td>
                  <td>{CATEGORY_LABELS[p.category] ?? p.category}</td>
                  <td className="cell-muted">{p.models.join(', ') || '—'}</td>
                  <td>
                    {p.is_published ? (
                      <span className="tag tag-success">已发布</span>
                    ) : (
                      <span className="tag">草稿</span>
                    )}
                  </td>
                  <td className="cell-muted">{p.view_count}</td>
                  <td className="cell-muted">{dayjs(p.created_at).format('MM-DD HH:mm')}</td>
                  <td>
                    <div className="cell-actions">
                      <button className="btn btn-sm" onClick={() => openEdit(p)}>
                        编辑
                      </button>
                      <button className="btn btn-sm" onClick={() => handlePublish(p)}>
                        {p.is_published ? '下架' : '发布'}
                      </button>
                      <button className="btn btn-sm btn-danger" onClick={() => handleDelete(p)}>
                        删除
                      </button>
                    </div>
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

      {showModal && (
        <PromptModal
          initial={editTarget}
          onClose={() => setShowModal(false)}
          onSaved={handleModalSaved}
          onUnauthorized={handleUnauthorized}
        />
      )}
    </>
  )
}

// ---------- 新建 / 编辑弹窗 ----------

interface PromptModalProps {
  initial: AdminPrompt | null
  onClose: () => void
  onSaved: () => void
  onUnauthorized: () => void
}

function PromptModal({ initial, onClose, onSaved, onUnauthorized }: PromptModalProps) {
  const isEdit = initial !== null

  const [form, setForm] = useState<PromptCreateInput>({
    title: initial?.title ?? '',
    prompt_text: initial?.prompt_text ?? '',
    description: initial?.description ?? '',
    category: initial?.category ?? Object.keys(CATEGORY_LABELS)[0],
    models: initial?.models ?? [],
    author_name: initial?.author_name ?? '',
    source_url: initial?.source_url ?? '',
    is_published: initial?.is_published ?? false,
  })
  const [modelsInput, setModelsInput] = useState((initial?.models ?? []).join(', '))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSaving(true)
    setError('')
    const body: PromptCreateInput = {
      ...form,
      models: modelsInput
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      description: form.description || null,
      author_name: form.author_name || null,
      source_url: form.source_url || null,
    }
    try {
      if (isEdit && initial) {
        await updatePrompt(initial.id, body)
      } else {
        await createPrompt(body)
      }
      onSaved()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized()
      else setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  function set(key: keyof PromptCreateInput, val: unknown) {
    setForm((f) => ({ ...f, [key]: val }))
  }

  return (
    <div className="modal-mask" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-header">
          <span>{isEdit ? '编辑内容' : '新建内容'}</span>
          <button className="btn btn-sm" onClick={onClose}>
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            {error && <div className="alert alert-error">{error}</div>}

            <div className="field">
              <label>标题 *</label>
              <input
                required
                value={form.title}
                onChange={(e) => set('title', e.target.value)}
              />
            </div>

            <div className="field">
              <label>提示词原文 *</label>
              <textarea
                required
                rows={4}
                value={form.prompt_text}
                onChange={(e) => set('prompt_text', e.target.value)}
              />
            </div>

            <div className="field-row">
              <div className="field">
                <label>分类 *</label>
                <select value={form.category} onChange={(e) => set('category', e.target.value)}>
                  {Object.entries(CATEGORY_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>模型（逗号分隔）</label>
                <input
                  value={modelsInput}
                  onChange={(e) => setModelsInput(e.target.value)}
                  placeholder="即梦, Midjourney"
                />
              </div>
            </div>

            <div className="field-row">
              <div className="field">
                <label>作者名称</label>
                <input
                  value={form.author_name ?? ''}
                  onChange={(e) => set('author_name', e.target.value)}
                />
              </div>
              <div className="field">
                <label>来源链接</label>
                <input
                  value={form.source_url ?? ''}
                  onChange={(e) => set('source_url', e.target.value)}
                />
              </div>
            </div>

            <div className="field">
              <label>简介</label>
              <textarea
                rows={2}
                value={form.description ?? ''}
                onChange={(e) => set('description', e.target.value)}
              />
            </div>

            <div className="field">
              <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={form.is_published}
                  onChange={(e) => set('is_published', e.target.checked)}
                />
                保存后直接发布
              </label>
            </div>
          </div>

          <div className="modal-footer">
            <button type="button" className="btn" onClick={onClose}>
              取消
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
