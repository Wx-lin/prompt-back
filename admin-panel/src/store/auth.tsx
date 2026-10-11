/**
 * 登录态管理：用一个极简的 React Context 保存"是否已登录"。
 *
 * 这里没有引入状态管理库（Redux/Zustand），因为这个后台的状态极其简单：
 * 只有"管理员密钥"这一个全局状态，其余都是各页面自己的局部 state。
 * 引入状态库反而增加理解成本。等以后状态真的复杂了再换。
 */

import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import { clearAdminKey, getAdminKey, setAdminKey } from '../api/client'

interface AuthContextValue {
  /** 当前是否已登录（只代表"本地存了密钥"，密钥是否有效由第一次请求的结果决定） */
  isAuthenticated: boolean
  login: (key: string) => void
  logout: () => void
  /** 请求返回 401 时调用，清掉本地密钥并退回登录页 */
  handleUnauthorized: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  // 初始值直接读 sessionStorage：刷新页面后如果之前登录过，不用重新输密钥
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(() => Boolean(getAdminKey()))

  const login = useCallback((key: string) => {
    setAdminKey(key)
    setIsAuthenticated(true)
  }, [])

  const logout = useCallback(() => {
    clearAdminKey()
    setIsAuthenticated(false)
  }, [])

  const handleUnauthorized = useCallback(() => {
    clearAdminKey()
    setIsAuthenticated(false)
  }, [])

  return (
    <AuthContext.Provider value={{ isAuthenticated, login, logout, handleUnauthorized }}>
      {children}
    </AuthContext.Provider>
  )
}

/** 在组件里读取登录态；未包裹 Provider 时直接报错，避免静默拿到 undefined */
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth 必须在 AuthProvider 内部使用')
  }
  return ctx
}
