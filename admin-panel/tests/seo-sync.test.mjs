import test from 'node:test'
import assert from 'node:assert/strict'
import { syncPresentation } from '../src/api/seo-sync.ts'

test('unconfigured Google is never presented as connected or ready to sync', () => {
 assert.deepEqual(syncPresentation('google', null), {label:'未配置自动同步',description:'尚未完成 Google Search Console 授权配置，可继续人工导入 CSV。',canSync:false})
})
test('Baidu remains explicitly manual even if a malformed Google status is returned', () => {
 assert.deepEqual(syncPresentation('baidu', {configured:true,status:'success'}), {label:'仅人工导入',description:'百度尚未接入自动同步，请从百度搜索资源平台导出 CSV 后导入。',canSync:false})
})
test('configured Google allows retry after failure but forbids duplicate running jobs', () => {
 assert.equal(syncPresentation('google', {configured:true,status:'failed'}).canSync,true)
 assert.equal(syncPresentation('google', {configured:true,status:'failed'}).label,'最近同步失败')
 assert.equal(syncPresentation('google', {configured:true,status:'running'}).canSync,false)
 assert.equal(syncPresentation('google', {configured:true,status:'running'}).label,'同步中')
})
test('configured Google describes idle and successful states without fabricating metrics', () => {
 assert.equal(syncPresentation('google', {configured:true,status:'idle'}).label,'等待首次同步')
 assert.equal(syncPresentation('google', {configured:true,status:'success'}).label,'最近同步成功')
})
import { syncFailureReason } from '../src/api/seo-sync.ts'
test('safe error codes are translated and unknown codes remain diagnosable', () => {
 assert.equal(syncFailureReason('permission_denied'),'Google 账号无权访问该站点，请检查 Search Console 权限。')
 assert.equal(syncFailureReason('timeout'),'Google 请求超时，请稍后重试。')
 assert.equal(syncFailureReason('new_safe_code'),'同步失败（new_safe_code）')
})
test('external workflow mode never enables a server sync POST', () => {
 const view=syncPresentation('google',{configured:true,status:'success',mode:'external',trigger_available:false})
 assert.equal(view.canSync,false)
 assert.match(view.description,/由 GitHub Actions 每日同步/)
})
test('explicitly unavailable server trigger remains disabled', () => {
 assert.equal(syncPresentation('google',{configured:true,status:'idle',mode:'server',trigger_available:false}).canSync,false)
 assert.equal(syncPresentation('google',{configured:true,status:'idle',mode:'server',trigger_available:true}).canSync,true)
})
