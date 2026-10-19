import test from 'node:test'
import assert from 'node:assert/strict'
import { trafficChartOption } from '../src/api/traffic-chart.ts'

test('UV chart keeps zero visits, marks untracked history missing, and selects the last seven days', () => {
  const daily = Array.from({ length: 30 }, (_, i) => ({ date: `2026-10-${String(i + 1).padStart(2, '0')}`, uv: i === 28 ? 3 : 0 }))
  const copy = structuredClone(daily)
  const result = trafficChartOption(daily, 7, '2026-10-28T16:00:00Z')
  assert.equal(result.xAxis.data.length, 7)
  assert.deepEqual(result.series[0].data, [null, null, null, null, null, 3, 0])
  assert.deepEqual(daily, copy)
})
test('missing start date is not a month of observed zero traffic', () => {
  const result = trafficChartOption([{ date: '2026-10-19', uv: 0 }], 30, null)
  assert.deepEqual(result.series[0].data, [null])
})
