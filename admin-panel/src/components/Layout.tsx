/**
 * 侧边栏导航 + 整体布局容器。
 *
 * 用 React Router 的 NavLink 高亮当前路由，不需要自己管理 active 状态。
 * 右侧 main 区域直接渲染传入的 children（各功能页面组件）。
 */

import { NavLink } from 'react-router-dom'
import { type ReactNode } from 'react'
import { useAuth } from '../store/auth'

interface LayoutProps {
  children: ReactNode
}

export default function Layout({ children }: LayoutProps) {
  const { logout } = useAuth()

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="sidebar-brand">
          AIGC Prompt
          <small>后台管理</small>
        </div>

        <nav className="sidebar-nav">
          <NavLink to="/submissions" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>投稿审核</NavLink>
          <NavLink
            to="/prompts"
            className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
          >
            内容管理
          </NavLink>
          <NavLink
            to="/crawl-drafts"
            className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
          >
            采集草稿
          </NavLink>
          <NavLink
            to="/reports"
            className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
          >
            举报处理
          </NavLink>
          <NavLink to="/crawl-schedules" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>定时任务</NavLink>
        </nav>

        <div className="sidebar-footer">
          <button className="nav-item btn" style={{ width: '100%' }} onClick={logout}>
            退出登录
          </button>
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  )
}
