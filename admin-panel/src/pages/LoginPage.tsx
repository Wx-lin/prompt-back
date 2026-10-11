/**
 * 登录页：输入管理员密钥，验证通过后进入后台。
 *
 * "登录"不发送身份认证请求，只是把密钥存入 sessionStorage，并用一次真实的
 * GET /api/admin/prompts 请求来验证密钥是否有效——后端返回 401 则提示错误。
 * 这对"运营方自己在本机用"的场景完全够用。
 */

import { useState } from 'react'
import { useAuth } from '../store/auth'
import { ApiError } from '../api/client'
import { listPrompts } from '../api/admin'

export default function LoginPage() {
  const { login, logout } = useAuth()
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!key.trim()) {
      setError('请输入管理员密钥')
      return
    }
    setLoading(true)
    setError('')
    // 先临时写入密钥，让下面这次验证请求能带上 X-Admin-Key
    login(key.trim())
    try {
      // 用一次真实请求验证密钥是否有效，而不是直接假设成功
      await listPrompts({ page: 1, page_size: 1 })
      // 请求成功 → 密钥有效，login() 已经把状态写好了，什么都不用做
    } catch (err) {
      logout() // 密钥无效，清掉登录态退回本页
      if (err instanceof ApiError && err.status === 401) {
        setError('密钥不正确，请重新输入')
      } else {
        setError('连接后端失败，请确认后端服务已启动')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>AIGC Prompt</h1>
        <p className="subtitle">后台管理 · 仅限运营人员访问</p>

        <form onSubmit={handleSubmit}>
          {error && <div className="alert alert-error">{error}</div>}

          <div className="field">
            <label>管理员密钥</label>
            <input
              type="password"
              placeholder="输入 ADMIN_SECRET_KEY"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              autoFocus
            />
            <div className="field-hint">密钥在 .env 文件的 ADMIN_SECRET_KEY 字段</div>
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            disabled={loading}
            style={{ width: '100%', marginTop: 4 }}
          >
            {loading ? '验证中...' : '进入后台'}
          </button>
        </form>
      </div>
    </div>
  )
}
