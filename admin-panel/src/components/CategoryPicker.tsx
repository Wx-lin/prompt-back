import type { Category } from '../api/category-utils'
import { categoryLabel, isLegacyCategory } from '../api/category-utils'
export default function CategoryPicker({ categories, value, onChange, disabled, filter = false, required = !filter }: {
  categories: Category[]; value: string; onChange: (value: string) => void; disabled?: boolean; filter?: boolean; required?: boolean
}) {
  return <select aria-label={filter ? '分类筛选' : '分类'} value={value} required={required} disabled={disabled || !categories.length} onChange={event => onChange(event.target.value)}>
    <option value="">{!categories.length ? '分类目录未加载' : filter ? '全部分类' : '请选择分类'}</option>
    {isLegacyCategory(categories, value) && <option value={value} disabled>{categoryLabel(categories, value)}（旧分类，请重新选择）</option>}
    {categories.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
  </select>
}
