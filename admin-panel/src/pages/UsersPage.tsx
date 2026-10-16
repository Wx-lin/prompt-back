import { useEffect, useState } from 'react'
import { listUsers, type UserList } from '../api/insights'
import { ApiError } from '../api/client'
import { formatBeijingTime } from '../api/insights-utils'
import { useAuth } from '../store/auth'
import './insights.css'

export default function UsersPage() {
  const { handleUnauthorized } = useAuth()
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState({ search: '', state: '', page: 1 })
  const [data, setData] = useState<UserList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    // Request lifecycle resets when the query changes; cleanup aborts the previous request.
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError('')
    setData(null)
    listUsers({ search: query.search, is_active: query.state === '' ? undefined : query.state === 'true', page: query.page, page_size: 20 }, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setData(result) })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        if (err instanceof ApiError && err.status === 401) handleUnauthorized()
        else setError(err instanceof Error ? err.message : '加载失败，请重试')
      }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [query, revision, handleUnauthorized])
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return <div className="insights-page users-page">
    <header className="insights-hero">
      <div><div className="page-eyebrow">USER DIRECTORY</div><div className="insights-heading"><h1>用户管理</h1><span className="user-count-badge">{data ? `${data.all_users_total.toLocaleString()} 位用户` : '账号目录'}</span></div><p>了解每一位用户，掌握平台的增长起点。</p></div>
      <div className="insights-actions"><span className="insights-readonly">只读管理</span><button className="btn" disabled={loading} onClick={() => setRevision((v) => v + 1)}><RefreshIcon />{loading ? '刷新中…' : '刷新列表'}</button></div>
    </header>
    <section className="users-directory-panel">
      <div className="insights-panel-title"><div><h2>全部用户</h2><p>搜索账号并查看注册与登录信息</p></div><span className="insights-update">北京时间 · UTC+8</span></div>
      <form className="insights-filters" onSubmit={(event) => { event.preventDefault(); setQuery((current) => ({ ...current, search: searchInput.trim(), page: 1 })) }}>
        <label className="users-search"><span>昵称 / 邮箱</span><div className="users-search-input"><SearchIcon /><input value={searchInput} maxLength={200} placeholder="搜索昵称或邮箱…" onChange={(event) => setSearchInput(event.target.value)} /></div></label>
        <label><span>账号状态</span><select value={query.state} onChange={(event) => setQuery((current) => ({ ...current, state: event.target.value, page: 1 }))}><option value="">全部状态</option><option value="true">启用</option><option value="false">停用</option></select></label>
        <button className="btn btn-primary" type="submit">搜索</button><button className="btn users-reset" type="button" onClick={() => { setSearchInput(''); setQuery({ search: '', state: '', page: 1 }) }}>重置筛选</button>
      </form>
      {error && <div className="alert alert-error" role="alert">{error} <button className="btn btn-sm" onClick={() => setRevision((v) => v + 1)}>重试</button></div>}
      <div className="insights-list-summary">{data ? `筛选结果 ${data.total.toLocaleString()} 位 · 全站用户 ${data.all_users_total.toLocaleString()} 位` : loading ? '正在查询用户…' : '用户数据加载失败'}<span>账号状态不代表访问活跃度</span></div>
      <div className="insights-table-wrap"><table><thead><tr><th>用户</th><th>邮箱地址</th><th>账号状态</th><th>注册时间</th><th>最近登录</th></tr></thead><tbody>
        {loading && <tr><td colSpan={5} className="empty" role="status">正在加载账号…</td></tr>}
        {!loading && data?.items.length === 0 && <tr><td colSpan={5} className="empty"><div className="users-empty-icon"><SearchIcon /></div><strong>没有符合条件的用户</strong><p>试试其他关键词，或重置筛选条件。</p></td></tr>}
        {!loading && data?.items.map((user) => <tr key={user.id}><td><div className="user-identity"><span className="user-avatar" aria-hidden="true">{(user.nickname?.trim() || user.email || '?').slice(0, 1).toUpperCase()}</span><div><span className="user-name">{user.nickname?.trim() || '未填写'}</span><div className="user-meta" title={user.id}>ID: {user.id}</div></div></div></td><td className="user-email">{user.email?.trim() || '未填写'}</td><td><span className={`users-status ${user.is_active ? 'is-enabled' : 'is-disabled'}`}><i />{user.is_active ? '启用' : '停用'}</span></td><td className="insights-date">{formatBeijingTime(user.created_at)}</td><td className="insights-date">{formatBeijingTime(user.last_login_at)}</td></tr>)}
      </tbody></table></div>
      {data && <div className="pagination"><span>第 {data.page} / {pages} 页 · 每页 {data.page_size} 位</span><div className="users-pagination-actions"><button className="btn btn-sm" disabled={loading || query.page <= 1} onClick={() => setQuery((current) => ({ ...current, page: current.page - 1 }))}>上一页</button><span>{data.page}</span><button className="btn btn-sm" disabled={loading || query.page >= pages} onClick={() => setQuery((current) => ({ ...current, page: current.page + 1 }))}>下一页</button></div></div>}
    </section>
    <footer className="insights-footnote"><span>用户统计包含测试账号，未自动排除。</span><span>最近登录时间不等于访问活跃度</span></footer>
  </div>
}

function SearchIcon() {
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></svg>
}
function RefreshIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.4 6.7M20 4v7h-7" /></svg>
}
