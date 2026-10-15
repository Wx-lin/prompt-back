import test from 'node:test'
import assert from 'node:assert/strict'
import { normalizeCategories } from '../src/api/category-utils.ts'

test('an unavailable catalog does not turn a valid submitted key into a legacy category', async () => {
  const { isLegacyCategory } = await import('../src/api/category-utils.ts')
  assert.equal(isLegacyCategory([], 'portrait'), false)
  const catalog = normalizeCategories([{ key: 'portrait', label: '人像写真', label_en: 'Portraits', order: 1, count: 0 }])
  assert.equal(isLegacyCategory(catalog, 'portrait'), false)
  assert.equal(isLegacyCategory(catalog, 'workflow'), true)
  assert.equal(isLegacyCategory(catalog, ''), false)
})
