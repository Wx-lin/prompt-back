import ModelPicker from '../components/ModelPicker'
import { isActiveTask } from '../api/crawl-utils'
/**
 * 采集草稿页：查看爬虫采集回来的笔记/动态（支持小红书、微博、快手多平台），
 * 人工筛选后转为正式内容。
 *
 * 工作流：
 * 1. 列表筛选 status=pending 的草稿
 * 2. 顶部"一键采集"面板可多选平台 + 关键词 + 数量，点击后按平台排队依次采集
 * 3. 点"查看/审核"打开详情弹窗，看完整原文和配图
 * 4. 在详情弹窗里填写"整理后的提示词/分类/模型"（PATCH 接口），随时可以先保存再继续
 * 5. 确认无误后点"通过并入库"，输入最终标题，草稿转为正式 Prompt（默认草稿态）
 * 6. 觉得不合格点"忽略"，草稿标记为 rejected
 *
 * 对应后端接口：app/routers/admin.py 的 /crawl-drafts 和 /crawl-tasks 系列端点。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import dayjs from 'dayjs'
import TaskLoginDialog from '../components/TaskLoginDialog'
import {
  listCrawlDrafts,
  updateCrawlDraft,
  approveCrawlDraft,
  rejectCrawlDraft,
  startCrawlTask,
  getCrawlTask,
  listCrawlPlatforms,
} from '../api/admin'
import type { CrawlDraft, CrawlPlatformInfo, CrawlTask, Paginated } from '../api/types'
import { CATEGORY_LABELS, DRAFT_STATUS_LABELS, PLATFORM_LABELS, TASK_STATUS_LABELS } from '../api/types'
import { ApiError } from '../api/client'
import { useAuth } from '../store/auth'

const PAGE_SIZE = 20

const FILTER_OPTIONS = [
  { value: 'pending', label: '待审核' },
  { value: '', label: '全部' },
  { value: 'approved', label: '已通过' },
  { value: 'rejected', label: '已忽略' },
]

export default function CrawlDraftsPage() {
  const { handleUnauthorized } = useAuth()

  const [data, setData] = useState<Paginated<CrawlDraft> | null>(null)
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState('pending')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const notifiedLoginTasks = useRef(new Set<string>())
  const [loginTask, setLoginTask] = useState<string | null>(null)
  const [reviewing, setReviewing] = useState<CrawlDraft | null>(null)

  // 一批任务（多选平台时每个平台一条），key 是 task.id
  const [batchTasks, setBatchTasks] = useState<Map<string, CrawlTask>>(new Map())
  const pollTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  const fetchData = useCallback(
    async (p: number, f: string) => {
      setLoading(true)
      setError('')
      try {
        const res = await listCrawlDrafts({ status: f || undefined, page: p, page_size: PAGE_SIZE })
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

  // 组件卸载时清掉所有还没触发的轮询计时器，避免内存泄漏/操作已卸载组件的状态
  useEffect(() => {
    return () => {
      for (const timer of pollTimersRef.current.values()) clearTimeout(timer)
    }
  }, [])

  /** 每 3 秒轮询一次单个任务状态，直到 success/failed 为止 */
  function pollTask(taskId: string) {
    const timer = setTimeout(async () => {
      try {
        const task = await getCrawlTask(taskId)
        setBatchTasks((prev) => new Map(prev).set(taskId, task))
        if (task.status === 'awaiting_login' && !notifiedLoginTasks.current.has(task.id)) {
          notifiedLoginTasks.current.add(task.id)
          setLoginTask((prev) => prev ?? task.id)
        }
        if (isActiveTask(task.status)) {
          pollTask(taskId)
        } else {
          // 这个平台的采集结束（成功或失败），刷新草稿列表让新数据立刻可见
          fetchData(page, filter)
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) handleUnauthorized()
        // 轮询失败不弹错误打扰用户，直接停止这一个任务的轮询，避免死循环
      }
    }, 3000)
    pollTimersRef.current.set(taskId, timer)
  }

  async function handleStartCrawl(platforms: string[], keyword: string, limit: number) {
    try {
      const tasks = await startCrawlTask({ platforms, keyword, limit })
      setBatchTasks(new Map(tasks.map((t) => [t.id, t])))
      for (const task of tasks) pollTask(task.id)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) handleUnauthorized()
      else alert(err instanceof Error ? err.message : '启动采集失败')
    }
  }

  function statusTag(status: string) {
    if (status === 'pending') return <span className="tag tag-warning">待审核</span>
    if (status === 'approved') return <span className="tag tag-success">已通过</span>
    return <span className="tag">已忽略</span>
  }

  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <>
      <div className="page-header">
        <div>
          <h1>采集草稿</h1>
          <div className="subtitle">
            爬虫从各平台采集回来的笔记/动态，人工审核后可转为正式内容
          </div>
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

      <CrawlTaskPanel batchTasks={batchTasks} onStart={handleStartCrawl} onLogin={setLoginTask} />
      {loginTask && <TaskLoginDialog taskId={loginTask} onClose={() => setLoginTask(null)} />}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>原始标题</th>
              <th>平台</th>
              <th>作者</th>
              <th>配图数</th>
              <th>状态</th>
              <th>采集时间</th>
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
                  {filter === 'pending' ? '暂无待审核草稿，可以用上方面板开始采集' : '暂无数据'}
                </td>
              </tr>
            )}
            {!loading &&
              data?.items.map((d) => (
                <tr key={d.id}>
                  <td className="cell-title" title={d.raw_title ?? undefined}>
                    {d.raw_title ?? <span className="cell-muted">（无标题）</span>}
                  </td>
                  <td>{PLATFORM_LABELS[d.source_platform] ?? d.source_platform}</td>
                  <td className="cell-muted">{d.author_name ?? '—'}</td>
                  <td className="cell-muted">{d.images.length}</td>
                  <td>{statusTag(d.status)}</td>
                  <td className="cell-muted">{dayjs(d.created_at).format('MM-DD HH:mm')}</td>
                  <td>
                    <div className="cell-actions">
                      <button
                        className="btn btn-sm btn-primary"
                        onClick={() => setReviewing(d)}
                      >
                        查看/审核
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

      {reviewing && (
        <ReviewModal
          draft={reviewing}
          onClose={() => setReviewing(null)}
          onDone={() => {
            setReviewing(null)
            fetchData(page, filter)
          }}
          onUnauthorized={handleUnauthorized}
        />
      )}
    </>
  )
}

// ---------- 审核弹窗 ----------

interface ReviewModalProps {
  draft: CrawlDraft
  onClose: () => void
  onDone: () => void
  onUnauthorized: () => void
}

function ReviewModal({ draft, onClose, onDone, onUnauthorized }: ReviewModalProps) {
  // 可编辑的审核字段（PATCH /crawl-drafts/{id}）
  const [promptText, setPromptText] = useState(draft.extracted_prompt_text ?? '')
  const [category, setCategory] = useState(
    draft.extracted_category ?? Object.keys(CATEGORY_LABELS)[0],
  )
  const [models, setModels] = useState(draft.extracted_models ?? '')

  // 通过入库时的额外字段
  const [approveTitle, setApproveTitle] = useState(draft.raw_title ?? '')
  const [approvePublish, setApprovePublish] = useState(false)

  // 弹窗内的操作状态
  const [saving, setSaving] = useState(false)
  const [approving, setApproving] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  const isPending = draft.status === 'pending'

  /** 保存审核字段（不入库，只是把整理好的字段存回草稿） */
  async function handleSave() {
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      await updateCrawlDraft(draft.id, {
        extracted_prompt_text: promptText || null,
        extracted_category: category || null,
        extracted_models: models || null,
      })
      setSaved(true)
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized()
      else setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  /** 通过并转为正式内容 */
  async function handleApprove() {
    if (!approveTitle.trim()) {
      setError('请填写正式标题')
      return
    }
    setApproving(true)
    setError('')
    try {
      await approveCrawlDraft(draft.id, {
        title: approveTitle.trim(),
        prompt_text: promptText || null,
        category: category || null,
        models: models
          ? models
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
          : null,
        is_published: approvePublish,
      })
      onDone()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized()
      else setError(err instanceof Error ? err.message : '通过操作失败')
    } finally {
      setApproving(false)
    }
  }

  /** 忽略这条草稿 */
  async function handleReject() {
    if (!confirm('确认忽略这条草稿？')) return
    setRejecting(true)
    setError('')
    try {
      await rejectCrawlDraft(draft.id)
      onDone()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthorized()
      else setError(err instanceof Error ? err.message : '忽略操作失败')
    } finally {
      setRejecting(false)
    }
  }

  return (
    <div className="modal-mask" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal modal-wide">
        <div className="modal-header">
          <span>
            审核草稿
            <span
              className={`tag ${draft.status === 'pending' ? 'tag-warning' : draft.status === 'approved' ? 'tag-success' : ''}`}
              style={{ marginLeft: 8 }}
            >
              {DRAFT_STATUS_LABELS[draft.status]}
            </span>
          </span>
          <button className="btn btn-sm" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
          {/* 左：原始内容 */}
          <div>
            <div className="field">
              <label>原始标题</label>
              <div style={{ padding: '4px 0', fontWeight: 500 }}>
                {draft.raw_title ?? <span className="cell-muted">（无标题）</span>}
              </div>
            </div>

            <div className="field">
              <label>
                来源 · {draft.source_platform} ·{' '}
                <a href={draft.source_url} target="_blank" rel="noreferrer">
                  查看原文
                </a>
              </label>
              <div className="cell-muted" style={{ padding: '2px 0' }}>
                作者：{draft.author_name ?? '—'}
              </div>
            </div>

            <div className="field">
              <label>原始正文</label>
              <div className="raw-content">{draft.raw_content}</div>
            </div>

            <div className="field">
              <label>配图（{draft.images.length} 张）</label>
              {draft.images.length === 0 ? (
                <div className="cell-muted">无配图</div>
              ) : (
                <div className="draft-images">
                  {draft.images
                    .slice()
                    .sort((a, b) => a.sort_order - b.sort_order)
                    .map((img) => (
                      <a key={img.id} href={img.image_url} target="_blank" rel="noreferrer">
                        <img src={img.image_url} alt="" />
                      </a>
                    ))}
                </div>
              )}
            </div>
          </div>

          {/* 右：整理后的字段 + 入库操作 */}
          <div>
            {error && <div className="alert alert-error">{error}</div>}
            {saved && <div className="alert alert-success">字段已保存</div>}

            <div className="field">
              <label>整理后的提示词原文</label>
              <textarea
                rows={5}
                value={promptText}
                onChange={(e) => setPromptText(e.target.value)}
                placeholder="从原始正文里提取出的提示词内容"
                disabled={!isPending}
              />
            </div>

            <div className="field-row">
              <div className="field">
                <label>分类</label>
                <select
                  value={category}
                  onChange={(e) => setCategory(e.target.value)}
                  disabled={!isPending}
                >
                  {Object.entries(CATEGORY_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <ModelPicker value={models.split(',').map(value => value.trim()).filter(Boolean)}
                  onChange={(value) => setModels(value.join(','))} disabled={!isPending} />
              </div>
            </div>

            {isPending && (
              <div style={{ marginBottom: 16 }}>
                <button className="btn" disabled={saving} onClick={handleSave}>
                  {saving ? '保存中...' : '暂存字段'}
                </button>
                <span className="field-hint" style={{ display: 'inline', marginLeft: 8 }}>
                  可以先保存，稍后再决定是否通过
                </span>
              </div>
            )}

            {isPending && (
              <>
                <hr style={{ border: 'none', borderTop: '1px solid var(--color-border)', margin: '16px 0' }} />

                <div className="field">
                  <label>正式标题（通过入库后显示给用户）*</label>
                  <input
                    value={approveTitle}
                    onChange={(e) => setApproveTitle(e.target.value)}
                    placeholder="通常从原始标题整理而来"
                  />
                </div>

                <div className="field">
                  <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <input
                      type="checkbox"
                      style={{ width: 'auto' }}
                      checked={approvePublish}
                      onChange={(e) => setApprovePublish(e.target.checked)}
                    />
                    入库后直接发布（否则仍为草稿，需在内容管理里单独发布）
                  </label>
                </div>
              </>
            )}

            {!isPending && (
              <div className="alert alert-info">
                此草稿已{DRAFT_STATUS_LABELS[draft.status]}，只读展示。
              </div>
            )}
          </div>
        </div>

        <div className="modal-footer">
          {isPending && (
            <>
              <button
                className="btn btn-danger"
                disabled={rejecting}
                onClick={handleReject}
              >
                {rejecting ? '处理中...' : '忽略'}
              </button>
              <button
                className="btn btn-primary"
                disabled={approving}
                onClick={handleApprove}
              >
                {approving ? '处理中...' : '通过并入库'}
              </button>
            </>
          )}
          <button className="btn" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------- 采集任务面板：多选平台，点按钮触发 MediaCrawler，分平台轮询展示进度 ----------

interface CrawlTaskPanelProps {
  batchTasks: Map<string, CrawlTask>
  onLogin: (id: string) => void
  onStart: (platforms: string[], keyword: string, limit: number) => void
}

function CrawlTaskPanel({ batchTasks, onStart, onLogin }: CrawlTaskPanelProps) {
  const [platformOptions, setPlatformOptions] = useState<CrawlPlatformInfo[]>([])
  const [selectedPlatforms, setSelectedPlatforms] = useState<string[]>([])
  const [keyword, setKeyword] = useState('AI头像提示词')
  const [limit, setLimit] = useState(10)
  const [loadError, setLoadError] = useState('')

  // 页面加载时拉一次可用平台列表（而不是硬编码），后端加/减平台时这里自动跟着变
  useEffect(() => {
    listCrawlPlatforms()
      .then((res) => {
        setPlatformOptions(res.platforms)
        setSelectedPlatforms(res.platforms.map((p) => p.value)) // 默认全选
      })
      .catch(() => setLoadError('加载平台列表失败，请刷新页面重试'))
  }, [])

  const tasks = Array.from(batchTasks.values())
  const isRunning = tasks.some((t) => isActiveTask(t.status))

  function togglePlatform(value: string) {
    setSelectedPlatforms((prev) =>
      prev.includes(value) ? prev.filter((p) => p !== value) : [...prev, value],
    )
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!keyword.trim() || selectedPlatforms.length === 0) return
    onStart(selectedPlatforms, keyword.trim(), limit)
  }

  const selectedWarnings = platformOptions.filter((p) => selectedPlatforms.includes(p.value) && p.warning)

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-header">一键采集</div>
      <div className="card-body">
        {loadError && <div className="alert alert-error">{loadError}</div>}

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label>采集平台（多选）</label>
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
              {platformOptions.map((p) => (
                <label
                  key={p.value}
                  style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 400 }}
                >
                  <input
                    type="checkbox"
                    style={{ width: 'auto' }}
                    checked={selectedPlatforms.includes(p.value)}
                    onChange={() => togglePlatform(p.value)}
                    disabled={isRunning}
                  />
                  {p.label}
                </label>
              ))}
            </div>
          </div>

          {selectedWarnings.map((p) => (
            <div key={p.value} className="alert alert-info" style={{ marginBottom: 10 }}>
              {p.label}：{p.warning}
            </div>
          ))}

          <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end', flexWrap: 'wrap' }}>
            <div className="field" style={{ marginBottom: 0, flex: '1 1 200px' }}>
              <label>搜索关键词</label>
              <input
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                disabled={isRunning}
                placeholder="例如：AI头像提示词"
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 140 }}>
              <label>每平台采集数量</label>
              <input
                type="number"
                min={1}
                max={50}
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value) || 10)}
                disabled={isRunning}
              />
            </div>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isRunning || selectedPlatforms.length === 0}
            >
              {isRunning ? '采集中...' : '开始采集'}
            </button>
          </div>
        </form>

        {tasks.length > 0 && (
          <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {tasks.map((task) => (
              <TaskStatusRow key={task.id} task={task} onLogin={onLogin} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/** 单个平台任务的状态行：排队中 / 采集中 / 已完成 / 失败，各自独立展示 */
function TaskStatusRow({ task, onLogin }: { task: CrawlTask; onLogin: (id: string) => void }) {
  const platformLabel = PLATFORM_LABELS[task.source_platform] ?? task.source_platform

  if (task.status === 'pending') {
    return (
      <div className="alert alert-info" style={{ marginBottom: 0 }}>
        【{platformLabel}】{TASK_STATUS_LABELS.pending}，等待前面的平台采集完成...
      </div>
    )
  }
  if (task.status === 'awaiting_login') return <div className="alert alert-info">【{platformLabel}】等待登录：请扫码后继续。 <button className="btn btn-sm btn-primary" onClick={() => onLogin(task.id)}>扫码登录</button></div>
  if (task.status === 'reviewing') return <div className="alert alert-info">【{platformLabel}】AI 审核中 · {task.stage || '正在审核采集内容'}</div>
  if (task.status === 'running') {
    return (
      <div className="alert alert-info" style={{ marginBottom: 0 }}>
        【{platformLabel}】正在采集关键词「{task.keyword}」，预计需要几十秒到几分钟...
        <div className="field-hint" style={{ marginTop: 4 }}>
          {task.stage || '需要登录时将自动显示扫码窗口。'}
        </div>
      </div>
    )
  }
  if (task.status === 'success') {
    return (
      <div className="alert alert-success" style={{ marginBottom: 0 }}>
        【{platformLabel}】采集完成！新增 {task.new_draft_count} 条待审核草稿，已在下方列表刷新。
      </div>
    )
  }
  return (
    <div className="alert alert-error" style={{ marginBottom: 0 }}>
      【{platformLabel}】采集失败：{task.error_message || '未知错误'}
      {task.log_tail && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer' }}>查看日志</summary>
          <div className="raw-content" style={{ marginTop: 6, maxHeight: 200 }}>
            {task.log_tail}
          </div>
        </details>
      )}
    </div>
  )
}
