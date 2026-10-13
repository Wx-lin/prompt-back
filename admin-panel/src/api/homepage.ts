import { apiRequest } from './client'
export interface FeaturedPrompt { id: string; title: string; thumbnail_url: string | null; category: string }
export interface FeaturedQueue { items: FeaturedPrompt[]; revision: string }
export const getFeatured = () => apiRequest<FeaturedQueue>('/api/admin/homepage/featured')
export const saveFeatured = (items: FeaturedPrompt[], revision: string) => apiRequest<FeaturedQueue>('/api/admin/homepage/featured', {
  method: 'PUT', body: JSON.stringify({ prompt_ids: items.map(item => item.id), revision }),
})
export const getCandidates = (q: string, page: number) => apiRequest<{ items: FeaturedPrompt[]; total: number; has_more: boolean }>(
  `/api/admin/homepage/candidates?${new URLSearchParams({ q, page: String(page), page_size: '20' })}`,
)
