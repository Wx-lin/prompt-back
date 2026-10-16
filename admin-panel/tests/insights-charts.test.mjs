import test from 'node:test'
import assert from 'node:assert/strict'
import { categoryChartOption, statusChartOption } from '../src/api/insights-charts.ts'

test('category chart retains every category, stable descending order, and published values', () => {
  const categories = [{ key: 'a', label: '空分类', total: 0, published: 0 }, { key: 'b', label: '人像', total: 12, published: 8 }, { key: 'c', label: '未知', total: 12, published: 0 }]
  const option = categoryChartOption(categories)
  assert.deepEqual(option.xAxis.data, ['人像', '未知', '空分类'])
  assert.deepEqual(option.series.map(s => s.data), [[12, 12, 0], [8, 0, 0]])
  assert.equal(categories[0].key, 'a')
  assert.equal(option.tooltip.renderMode, 'richText')
})
test('status chart faithfully accounts for all states and shows no artificial data for empty inventory', () => {
  const content = { total: 20, published: 8, pending: 5, rejected: 3, other_unpublished: 4, images: 30 }
  const option = statusChartOption(content)
  assert.equal(option.series[0].data.reduce((n, item) => n + item.value, 0), content.total)
  assert.deepEqual(statusChartOption({ ...content, total: 0, published: 0, pending: 0, rejected: 0, other_unpublished: 0 }).series[0].data.map(item => item.value), [0, 0, 0, 0])
})
