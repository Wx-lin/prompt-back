import test from 'node:test'
import assert from 'node:assert/strict'
import { normalizeCategories, isValidCategory, categoryLabel } from '../src/api/category-utils.ts'

test('API directory preserves zero stock, canonical labels and order without mutating response', () => {
  const input = [{ key: 'scene', label: '风景与静物', label_en: 'Scenery', order: 12, count: 0 }, { key: 'portrait', label: '人像写真', label_en: 'Portraits', order: 1, count: 2 }]
  const result = normalizeCategories(input)
  assert.deepEqual(result.map(item => item.key), ['portrait', 'scene'])
  assert.equal(result[1].count, 0)
  assert.equal(input[0].key, 'scene')
  assert.ok(isValidCategory(result, 'scene'))
  assert.equal(isValidCategory(result, 'workflow'), false)
  assert.equal(categoryLabel(result, 'portrait'), '人像写真')
  assert.equal(categoryLabel(result, 'workflow'), '工作流 & 技巧')
})
test('malformed or duplicate API catalog fails closed', () => {
  assert.throws(() => normalizeCategories([{ key: 'x' }]))
  const item = { key: 'x', label: 'X', label_en: 'X', order: 1, count: 0 }
  assert.throws(() => normalizeCategories([item, item]))
})
