import { NavLink, useLocation } from 'react-router-dom'
import { type ReactNode } from 'react'
import { useAuth } from '../store/auth'

const navigation = [
  { to: '/dashboard', label: '数据大盘', icon: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z' },
  { to: '/users', label: '用户管理', icon: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0' },
  { to: '/homepage', label: '首页推荐', icon: 'm12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z' },
  { to: '/submissions', label: '投稿审核', icon: 'M9 3h6v4H9zM9 5H5v16h14V5h-4M8 13l3 3 5-6' },
  { to: '/prompts', label: '内容管理', icon: 'M4 3h12l4 4v14H4zM14 3v6h6M8 13h8M8 17h6' },
  { to: '/crawl-drafts', label: '采集草稿', icon: 'M3 8h18v13H3zM7 8V3h10v5M8 13h8M10 17h4' },
  { to: '/reports', label: '举报处理', icon: 'M4 22V3c5-5 11 5 16 0v12c-5 5-11-5-16 0' },
  { to: '/crawl-schedules', label: '定时任务', icon: 'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0' },
]

export default function Layout({ children }: { children: ReactNode }) {
  const { logout } = useAuth()
  const { pathname } = useLocation()
  const insights = pathname === '/dashboard' || pathname === '/users'
  return <div className={`layout${insights ? ' insights-layout' : ''}`}>
    <aside className="sidebar">
      <div className="sidebar-brand">{insights && <span className="sidebar-mark" aria-hidden="true">A</span>}<span className="sidebar-brand-copy">AIGC Prompt<small>运营工作台</small></span></div>
      <nav className="sidebar-nav">{insights && <div className="sidebar-section-label">工作空间</div>}{navigation.map(item =>
        <NavLink key={item.to} to={item.to} aria-label={item.label} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>{insights && <NavIcon path={item.icon} />}<span>{item.label}</span></NavLink>)}
      </nav>
      <div className="sidebar-footer">{insights && <div className="sidebar-account"><span className="sidebar-account-avatar">O</span><div><strong>运营管理员</strong><small>Administrator</small></div></div>}<button className="nav-item btn" style={{ width: '100%' }} onClick={logout} aria-label="退出登录"><NavIcon path="M9 21H3V3h6M16 17l5-5-5-5M21 12H9" />退出登录</button></div>
    </aside><main className="main">{children}</main>
  </div>
}
function NavIcon({ path }: { path: string }) {
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={path} /></svg>
}
