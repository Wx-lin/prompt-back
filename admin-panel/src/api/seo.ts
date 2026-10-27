import type { SeoSyncStatus } from './seo-sync'
import { apiRequest } from './client'
import type { SearchDimension, SearchRow } from './seo-utils'
export type SearchEngine = 'google' | 'baidu'
export interface SearchImport {engine:SearchEngine;dimension:SearchDimension;start_date:string;end_date:string;rows:SearchRow[]}
interface Coverage {status:'missing'|'partial'|'complete'; start_date:string|null;end_date:string|null;row_count:number}
export interface SeoData {
 engine:SearchEngine;start_date:string;end_date:string;connection_status:'manual_import'|'not_connected'|'connected';sync?:SeoSyncStatus;source_timezone:string;updated_at:string|null;
 totals:{clicks:number;impressions:number;ctr:number;position:number|null}|null;
 daily:SearchRow[];queries:SearchRow[];pages:SearchRow[];coverage:Record<SearchDimension,Coverage>
}
export function getSeo(engine:SearchEngine,start:string,end:string,signal?:AbortSignal) {
 return apiRequest<SeoData>(`/api/admin/seo?${new URLSearchParams({engine,start_date:start,end_date:end})}`,{signal,cache:'no-store'})
}
export function importSeo(payload:SearchImport) {
 return apiRequest('/api/admin/seo/import',{method:'POST',body:JSON.stringify(payload)})
}

export function syncGoogleSeo() {
 return apiRequest<{accepted:boolean}>('/api/admin/seo/sync/google',{method:'POST',signal:AbortSignal.timeout(15000)})
}
export function getGoogleSeoSync(signal?:AbortSignal) {
 return apiRequest<SeoSyncStatus>('/api/admin/seo/sync/google/status',{signal:signal ? AbortSignal.any([signal,AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),cache:'no-store'})
}
