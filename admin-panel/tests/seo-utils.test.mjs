import test from 'node:test'
import assert from 'node:assert/strict'
import { parseSearchCsv, trendOption } from '../src/api/seo-utils.ts'
test('Google exported CSV handles BOM, quoted commas, CRLF and translated headers', () => {
  const rows = parseSearchCsv('\ufeff热门查询,点击次数,展示次数,点击率,平均排名\r\n"头像,胶片",2,100,2%,3.2\r\n', 'query')
  assert.deepEqual(rows, [{key:'头像,胶片',clicks:2,impressions:100,position:3.2}])
})
test('daily Chinese Baidu export allows absent rank and preserves genuine zero', () => {
  assert.deepEqual(parseSearchCsv('日期,点击量,展现量\n2026-10-14,0,0', 'daily'), [{key:'2026-10-14',clicks:0,impressions:0,position:null}])
})
test('reject invalid metrics and unsupported columns instead of inventing zeros', () => {
  for(const csv of ['Date,Clicks,Impressions\n2026-10-14,x,2','Date,Clicks,Impressions\n2026-10-14,3,2','Date,Clicks,Impressions\n2026-10-14,1,-1','Date,Clicks,Impressions\n2026-10-14,,2','Date,Clicks,Impressions\n"2026-10-14,1,2']) assert.throws(() => parseSearchCsv(csv,'daily'))
  assert.throws(() => parseSearchCsv('unknown,foo\na,b','query'))
})
test('trend keeps source date keys and never fills missing dates with zero', () => {
 const option = trendOption([{key:'2026-10-12',clicks:0,impressions:0,position:null},{key:'2026-10-14',clicks:1,impressions:5,position:null}])
 assert.deepEqual(option.xAxis.data,['2026-10-12','2026-10-14'])
 assert.deepEqual(option.series[0].data,[0,1])
})
import { createReadGate } from '../src/api/seo-utils.ts'
test('file read generation invalidates older completion and filter-context reads', () => {
 const gate=createReadGate(); const first=gate.begin(); assert.equal(first(),true)
 const second=gate.begin(); assert.equal(first(),false); assert.equal(second(),true)
 gate.cancel(); assert.equal(second(),false)
})
import { reportingWindow } from '../src/api/seo-utils.ts'
test('default window uses source complete days near UTC midnight',()=>{
 assert.deepEqual(reportingWindow('google',new Date('2026-10-16T01:00:00Z')),{start:'2026-09-17',end:'2026-10-14'})
 assert.deepEqual(reportingWindow('baidu',new Date('2026-10-16T01:00:00Z')),{start:'2026-09-18',end:'2026-10-15'})
})
