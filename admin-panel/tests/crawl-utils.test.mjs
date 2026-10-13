import { test } from 'node:test'
import assert from 'node:assert/strict'
import { beijingDate, beijingTime, isActiveTask, validateSchedule } from '../src/api/crawl-utils.ts'
const valid = { name: '每天采集', keyword: 'AI', platforms: ['xiaohongshu'], times: ['10:00', '22:00'], limit_count: 20 }
test('schedule permits midnight and max quantity, rejects duplicate/malformed times and quantity boundaries', () => {
  assert.equal(validateSchedule({ ...valid, times: ['00:00', '23:59'], limit_count: 50 }), '')
  for (const times of [[], ['24:00'], ['10:60'], ['1:00'], ['10:00', '10:00']]) assert.ok(validateSchedule({ ...valid, times }))
  for (const limit_count of [0, 51, 1.5, NaN]) assert.ok(validateSchedule({ ...valid, limit_count }))
  assert.ok(validateSchedule({ ...valid, platforms: [] }))
  assert.ok(validateSchedule({ ...valid, keyword: '  ' }))
})
test('login and review remain active, preventing overlapping manual submission', () => {
  for (const status of ['pending', 'running', 'awaiting_login', 'reviewing']) assert.equal(isActiveTask(status), true)
  for (const status of ['success', 'failed']) assert.equal(isActiveTask(status), false)
})
test('BJ day crosses UTC midnight and UTC timestamps display consistently', () => {
  assert.equal(beijingDate(new Date('2026-10-09T16:01:00Z')), '2026-10-10')
  assert.equal(beijingTime('2026-10-10T02:00:00'), beijingTime('2026-10-10T02:00:00Z'))
  assert.ok(beijingTime('2026-10-10T02:00:00Z').includes('10:00'))
  assert.equal(beijingTime(null), '—')
  assert.equal(beijingTime('invalid'), '—')
})

test('QR refresh waits for a new revision and cannot report login before awaiting-login', async () => {
  const { hasNewQrVersion, hasLoginResumed } = await import('../src/api/crawl-utils.ts')
  assert.equal(hasNewQrVersion(true, 3, 3), false)
  assert.equal(hasNewQrVersion(false, 4, 3), false)
  assert.equal(hasNewQrVersion(true, 4, 3), true)
  assert.equal(hasNewQrVersion(true, null, undefined), true)
  assert.equal(hasLoginResumed(false, false, 'running'), false)
  assert.equal(hasLoginResumed(true, true, 'awaiting_login'), false)
  assert.equal(hasLoginResumed(true, false, 'failed'), false)
  assert.equal(hasLoginResumed(true, false, 'reviewing'), true)
})

test('login polling ends on task completion or confirmed resume, never during scan wait', async () => {
  const { hasLoginFinished } = await import('../src/api/crawl-utils.ts')
  assert.equal(hasLoginFinished(false, true, 'failed'), true)
  assert.equal(hasLoginFinished(false, false, 'success'), true)
  assert.equal(hasLoginFinished(true, false, 'running'), true)
  assert.equal(hasLoginFinished(true, false, 'reviewing'), true)
  assert.equal(hasLoginFinished(false, false, 'running'), false)
  assert.equal(hasLoginFinished(true, true, 'awaiting_login'), false)
})

test('keyword editor trims blank lines, deduplicates, and validates list limits', async () => {
  const { parseScheduleKeywords } = await import('../src/api/crawl-utils.ts')
  assert.deepEqual(parseScheduleKeywords(' AI头像提示词 \n\n短剧人物提示词\nAI头像提示词 '), ['AI头像提示词', '短剧人物提示词'])
  assert.equal(validateSchedule({ ...valid, keywords: ['AI', '头像'] }), '')
  assert.ok(validateSchedule({ ...valid, keywords: [''] }))
  assert.ok(validateSchedule({ ...valid, keywords: ['AI,头像'] }))
  assert.ok(validateSchedule({ ...valid, keywords: ['a'.repeat(201)] }))
  assert.ok(validateSchedule({ ...valid, keywords: Array.from({ length: 31 }, (_, i) => `词${i}`) }))
  assert.equal(validateSchedule({ ...valid, keywords: [] }), '')
})
