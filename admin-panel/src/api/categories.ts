import { useEffect, useState } from 'react'
import { apiRequest } from './client'
import { normalizeCategories, type Category } from './category-utils'
export function useCategories() {
  const [categories, setCategories] = useState<Category[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    apiRequest<unknown>('/api/admin/categories').then(normalizeCategories).then(result => { if (active) setCategories(result) })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : '分类加载失败') })
    return () => { active = false }
  }, [])
  return { categories, error }
}
