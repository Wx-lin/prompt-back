export interface SeoSyncStatus {
 mode?: 'server'|'external'
 trigger_available?: boolean
 configured: boolean
 status: 'not_configured'|'idle'|'running'|'success'|'failed'
 last_attempt_at: string|null
 last_success_at: string|null
 last_error: string|null
 data_through: string|null
 truncated: boolean
}
export function syncPresentation(engine: 'google'|'baidu', sync?: Pick<SeoSyncStatus,'configured'|'status'|'mode'|'trigger_available'>|null) {
 if(engine === 'baidu') return {label:'仅人工导入',description:'百度尚未接入自动同步，请从百度搜索资源平台导出 CSV 后导入。',canSync:false}
 if(!sync?.configured) return {label:'未配置自动同步',description:'尚未完成 Google Search Console 授权配置，可继续人工导入 CSV。',canSync:false}
 const labels = {not_configured:'未配置自动同步',idle:'等待首次同步',running:'同步中',success:'最近同步成功',failed:'最近同步失败'}
 return {label:labels[sync.status],description:sync.mode==='external' ? '由 GitHub Actions 每日同步，数据按来源报表日期更新，可能存在平台延迟。' : 'Google Search Console 定时同步，数据按来源报表日期更新，可能存在平台延迟。',canSync:sync.mode!=='external' && sync.trigger_available!==false && sync.status !== 'running' && sync.status !== 'not_configured'}
}
export function syncFailureReason(code:string):string {
 const reasons:Record<string,string> = {
  credentials_unavailable:'Google 授权凭据不可用，请检查服务器配置。',
  permission_denied:'Google 账号无权访问该站点，请检查 Search Console 权限。',
  upstream_unavailable:'Google 服务暂时不可用，请稍后重试。',
  upstream_rejected:'Google 拒绝了同步请求，请检查站点配置与配额。',
  invalid_response:'Google 返回的数据格式不符合预期，本次未写入报表。',
  timeout:'Google 请求超时，请稍后重试。',
  interrupted:'上次同步被中断，请重新同步。',
  sync_failed:'同步失败，请检查服务器日志后重试。',
 }
 return Object.hasOwn(reasons,code) ? reasons[code] : `同步失败（${code}）`
}
