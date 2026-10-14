export interface Category { key: string; label: string; label_en: string; order: number; count: number }
const LEGACY_LABELS: Record<string, string> = { photography: '模拟摄影', image_edit: '图片编辑', workflow: '工作流 & 技巧' }
export function normalizeCategories(value: unknown): Category[] {
  if (!Array.isArray(value) || !value.length) throw new Error('分类目录不可用')
  const keys = new Set<string>()
  for (const item of value) {
    if (!item || typeof item.key !== 'string' || !item.key || typeof item.label !== 'string' || typeof item.label_en !== 'string' || !Number.isFinite(item.order) || !Number.isInteger(item.count) || item.count < 0 || keys.has(item.key)) throw new Error('分类目录格式错误')
    keys.add(item.key)
  }
  return value.map(item => ({ ...item })).sort((a, b) => a.order - b.order)
}
export function isValidCategory(categories: Category[], key: string): boolean { return categories.some(item => item.key === key) }
export function categoryLabel(categories: Category[], key: string): string { return categories.find(item => item.key === key)?.label ?? LEGACY_LABELS[key] ?? key }
