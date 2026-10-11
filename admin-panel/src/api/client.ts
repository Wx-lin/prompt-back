/**
 * HTTP 请求封装：统一处理管理员密钥注入、错误解析、JSON 序列化。
 *
 * 鉴权方式对应后端 app/utils/admin_auth.py：所有 /api/admin/* 接口都要求
 * X-Admin-Key 请求头。密钥存在 sessionStorage（关闭标签页即失效），
 * 不做"记住我"——这是运营方自己用的内部工具，够用即可。
 *
 * 为什么用 sessionStorage 而不是 localStorage：这台电脑可能是共用的，
 * 密钥长期留在浏览器里风险更大；而且运营方不会希望密钥跟着浏览器永久保存。
 */

/** 后端地址：开发环境走 Vite 代理（空字符串 = 同源），生产环境可通过环境变量覆盖 */
const API_BASE = import.meta.env.VITE_API_BASE || ''

const KEY_STORAGE = 'admin_key'

/** 读取当前管理员密钥 */
export function getAdminKey(): string {
  return sessionStorage.getItem(KEY_STORAGE) || ''
}

/** 写入管理员密钥 */
export function setAdminKey(key: string): void {
  sessionStorage.setItem(KEY_STORAGE, key)
}

/** 清除管理员密钥 */
export function clearAdminKey(): void {
  sessionStorage.removeItem(KEY_STORAGE)
}

/** 请求失败时抛出的错误，带上 HTTP 状态码供调用方区分处理（如 401 跳登录） */
export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * 统一请求函数。
 *
 * 后端返回的错误体格式是 FastAPI 默认的 {"detail": "..."}，这里统一拆出来
 * 变成 Error.message，调用方拿到的就是可以直接展示给用户的中文提示。
 */
export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const res = await fetch(API_BASE + path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Key': getAdminKey(),
      ...(options.headers || {}),
    },
  })

  if (!res.ok) {
    let detail = `请求失败（HTTP ${res.status}）`
    try {
      const data = await res.json()
      if (data?.detail) {
        // detail 可能是字符串，也可能是 FastAPI 校验错误的数组，统一转成可读文本
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
      }
    } catch {
      // 响应体不是 JSON（如 502 网关错误页），保留上面的默认提示
    }
    throw new ApiError(res.status, detail)
  }

  // 204 No Content（删除接口）没有响应体，不能调 json()
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

/** QR 图片同样通过请求头鉴权，密钥不会出现在图片 URL。 */
export async function apiBlob(path: string): Promise<Blob> {
  const res = await fetch(API_BASE + path, { cache: 'no-store', headers: { 'X-Admin-Key': getAdminKey() } })
  if (!res.ok) throw new ApiError(res.status, `二维码读取失败（HTTP ${res.status}）`)
  return res.blob()
}
