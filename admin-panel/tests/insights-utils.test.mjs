import test from 'node:test'
import assert from 'node:assert/strict'
import { formatBeijingTime, userSearchParams } from '../src/api/insights-utils.ts'

test('dates use Beijing time across day boundary with safe null fallback', () => {
  assert.equal(formatBeijingTime('2026-10-15T18:04:05Z'), '2026-10-16 02:04:05')
  assert.equal(formatBeijingTime(null), '暂无')
  assert.equal(formatBeijingTime('invalid'), '暂无')
})
test('user query trims literal search and preserves disabled and bounded pagination', () => {
  const params = userSearchParams({ search: '  a%_+@  ', is_active: false, page: 2, page_size: 20 })
  assert.equal(params.get('search'), 'a%_+@')
  assert.equal(params.get('is_active'), 'false')
  assert.equal(params.get('page'), '2')
  assert.equal(userSearchParams({ page: 1, page_size: 20 }).has('is_active'), false)
})
