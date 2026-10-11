import { useEffect, useRef, useState } from 'react'
import { getTaskLogin, refreshTaskLogin } from '../api/admin'
import { apiBlob } from '../api/client'
import { hasNewQrVersion, hasLoginResumed } from '../api/crawl-utils'
import type { TaskLoginState } from '../api/types'

export default function TaskLoginDialog({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const [state, setState] = useState<TaskLoginState | null>(null)
  const [qr, setQr] = useState('')
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [resumed, setResumed] = useState(false)
  const refreshBaseline = useRef<{ version: string | number | null } | null>(null)
  const sawAwaitingLogin = useRef(false)
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => {
    let alive = true
    let url = ''
    let timer: ReturnType<typeof setTimeout>
    let fetchedVersion: string | number | null | undefined
    async function poll() {
      try {
        const next = await getTaskLogin(taskId)
        if (!alive) return
        if (next.status === 'awaiting_login' && next.required) sawAwaitingLogin.current = true
        setState(next)
        setResumed(hasLoginResumed(sawAwaitingLogin.current, next.required, next.status))
        if (!next.qr_available) {
          if (url) URL.revokeObjectURL(url)
          url = ''
          fetchedVersion = undefined
          setQr('')
        }
        const waiting = refreshBaseline.current
        const freshAfterRefresh = !waiting || hasNewQrVersion(next.qr_available, next.qr_version, waiting.version)
        if (next.qr_available && freshAfterRefresh && (!url || hasNewQrVersion(true, next.qr_version, fetchedVersion))) {
          const blob = await apiBlob(`/api/admin/crawl-tasks/${taskId}/login-qr`)
          if (!alive) return
          if (refreshBaseline.current && !hasNewQrVersion(true, next.qr_version, refreshBaseline.current.version)) {
            timer = setTimeout(poll, 2500)
            return
          }
          const newUrl = URL.createObjectURL(blob)
          if (url) URL.revokeObjectURL(url)
          url = newUrl
          fetchedVersion = next.qr_version
          setQr(url)
          refreshBaseline.current = null
          clearTimeout(refreshTimer.current)
          setError('')
          setRefreshing(false)
        }
        if (next.status === 'failed' || hasLoginResumed(sawAwaitingLogin.current, next.required, next.status)) {
          refreshBaseline.current = null
          clearTimeout(refreshTimer.current)
          setRefreshing(false)
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : '登录状态读取失败')
      }
      if (alive) timer = setTimeout(poll, 2500)
    }
    void poll()
    return () => { alive = false; clearTimeout(timer); clearTimeout(refreshTimer.current); if (url) URL.revokeObjectURL(url) }
  }, [taskId])
  async function refresh() {
    refreshBaseline.current = { version: state?.qr_version ?? null }
    setRefreshing(true)
    setError('')
    setQr('')
    clearTimeout(refreshTimer.current)
    refreshTimer.current = setTimeout(() => {
      refreshBaseline.current = null
      setRefreshing(false)
      setError('刷新超时，服务器尚未生成新的二维码，请重试。')
    }, 15000)
    try { await refreshTaskLogin(taskId) }
    catch (err) {
      refreshBaseline.current = null
      clearTimeout(refreshTimer.current)
      setError(err instanceof Error ? err.message : '刷新失败')
      setRefreshing(false)
    }
  }
  const expired = state?.required && state.expires_at && new Date(state.expires_at).getTime() <= Date.now()
  return <div className="modal-mask"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="login-title" style={{ maxWidth: 440 }}>
    <div className="modal-header"><span id="login-title">平台扫码登录</span><button className="btn btn-sm" onClick={onClose}>关闭</button></div>
    <div className="modal-body" style={{ textAlign: 'center' }}>
      {error && <div className="alert alert-error">{error}</div>}
      {resumed ? <div className="alert alert-success">登录已确认，采集任务已继续。可以关闭此窗口。</div> : <>
        <p>{state?.message || '正在读取登录状态…'}</p>
        {qr && state?.required && state.qr_available ? <img src={qr} alt="平台登录二维码" style={{ width: 260, maxWidth: '100%' }} /> : <p className="cell-muted">{state?.status === 'failed' ? '任务已结束，请重新开始采集。' : refreshing ? '正在等待新的二维码…' : expired ? '二维码已过期，请刷新' : state?.status === 'success' ? '采集任务已完成。' : '二维码准备中…'}</p>}
        {state?.expires_at && <p className="cell-muted">请尽快扫码并在手机上确认；过期可刷新。</p>}
        <p className="field-hint">登录状态仅保存在服务器。扫码后此窗口会自动确认结果。</p>
      </>}
    </div>
    <div className="modal-footer">{!resumed && <button className="btn btn-primary" disabled={refreshing || state?.status === 'failed'} onClick={refresh}>{refreshing ? '刷新中…' : '刷新二维码'}</button>}<button className="btn" onClick={onClose}>{resumed ? '完成' : '稍后登录'}</button></div>
  </section></div>
}
