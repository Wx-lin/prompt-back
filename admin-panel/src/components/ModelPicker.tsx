import { useEffect, useId, useState } from 'react'
import { apiRequest } from '../api/client'

type ModelOption = { key: string; label: string; group: string }

export default function ModelPicker({ value, onChange, disabled = false }: {
  value: string[]; onChange: (value: string[]) => void; disabled?: boolean
}) {
  const id = useId()
  const [options, setOptions] = useState<ModelOption[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    apiRequest<ModelOption[]>('/api/models').then(items => {
      if (active) setOptions(items)
    }).catch(() => { if (active) setError('模型目录加载失败，已有标签将保留，请刷新重试。') })
    return () => { active = false }
  }, [])
  const unknown = value.filter(key => !options.some(item => item.key === key))
  const groups = [...new Set(options.map(item => item.group))]
  return <>
    <label htmlFor={id}>适用模型 / 版本（可多选）</label>
    <select id={id} multiple size={7} value={value} disabled={disabled}
      onChange={event => onChange(Array.from(event.target.selectedOptions, option => option.value))}>
      {groups.map(group => <optgroup key={group} label={group}>
        {options.filter(item => item.group === group).map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
      </optgroup>)}
      {unknown.length > 0 && <optgroup label="现有标签（保留）">{unknown.map(key => <option key={key} value={key}>{key}</option>)}</optgroup>}
    </select>
    <small>按住 Ctrl / ⌘ 可多选；未标注时可以留空。</small>
    {error && <div role="alert" className="alert alert-error">{error}</div>}
  </>
}
