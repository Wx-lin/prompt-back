export type SearchDimension = 'daily' | 'query' | 'page'
export interface SearchRow { key: string; clicks: number; impressions: number; position: number | null; ctr?: number }

function csvCells(text: string): string[][] {
  const rows: string[][] = []; let cells: string[] = []; let cell = ''; let quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === '"') {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++ } else quoted = !quoted
    } else if (!quoted && (c === ',' || c === '\n' || c === '\r')) {
      cells.push(cell.trim()); cell = ''
      if (c !== ',') { if (cells.some(Boolean)) rows.push(cells); cells = []; if(c === '\r' && text[i + 1] === '\n') i++ }
    } else cell += c
  }
  if (quoted) throw new Error('CSV 引号不完整')
  cells.push(cell.trim()); if(cells.some(Boolean)) rows.push(cells)
  return rows
}
export function parseSearchCsv(text: string, dimension: SearchDimension): SearchRow[] {
  const [headers, ...data] = csvCells(text.replace(/^\uFEFF/, ''))
  if (!headers || data.length === 0 || data.length > 5000) throw new Error('CSV 需要 1–5000 行数据')
  const find = (names: string[]) => headers.findIndex(h => names.includes(h.toLowerCase()))
  const key = find(dimension === 'daily' ? ['date','日期'] : dimension === 'query' ? ['top queries','query','queries','热门查询','查询','关键词','热门关键词'] : ['top pages','page','pages','热门网页','网页','页面','网址'])
  const clicks = find(['clicks','点击次数','点击量','点击']); const impressions = find(['impressions','展示次数','展现量','展示量','展现次数'])
  const position = find(['position','average position','平均排名','排名'])
  if (key < 0 || clicks < 0 || impressions < 0) throw new Error('找不到所选维度、点击或展示列，请使用站长平台导出的 CSV')
  return data.map((cells, index) => {
    const number = (column: number): number => {
      const raw = cells[column]?.replaceAll(',', '')
      if (!raw || !/^\d+(\.\d+)?$/.test(raw)) throw new Error(`第 ${index + 2} 行数值无效`)
      const value = Number(raw)
      if(!Number.isFinite(value)) throw new Error(`第 ${index + 2} 行数值无效`)
      return value
    }
    const row = {key:cells[key], clicks:number(clicks), impressions:number(impressions), position:position < 0 || !cells[position] ? null : number(position)}
    if (!row.key || !Number.isInteger(row.clicks) || !Number.isInteger(row.impressions) || row.clicks > row.impressions || (row.position !== null && row.position < 1)) throw new Error(`第 ${index + 2} 行数据无效`)
    return row
  })
}
export function trendOption(rows: SearchRow[]) {
  return {
    aria:{enabled:true}, tooltip:{trigger:'axis' as const,renderMode:'richText' as const}, legend:{data:['点击','曝光']},
    grid:{left:50,right:50,top:40,bottom:40},
    xAxis:{type:'category' as const,data:rows.map(row => row.key)},
    yAxis:[{type:'value' as const,minInterval:1},{type:'value' as const,minInterval:1}],
    series:[{name:'点击',type:'bar' as const,data:rows.map(row => row.clicks),itemStyle:{color:'#6366f1'}},{name:'曝光',type:'bar' as const,yAxisIndex:1,data:rows.map(row => row.impressions),itemStyle:{color:'#b9c9f5'}}],
  }
}
/** Each read captures its context; older files and filter changes invalidate it. */
export function createReadGate() {
  let generation = 0
  return { begin: () => { const current = ++generation; return () => current === generation }, cancel: () => { generation++ } }
}
export function reportingWindow(engine: 'google'|'baidu', now = new Date()) {
 const parts = new Intl.DateTimeFormat('en-US',{timeZone:engine==='google'?'America/Los_Angeles':'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(now)
 const part = (key:string) => parts.find(p=>p.type===key)?.value
 const sourceDay=Date.parse(`${part('year')}-${part('month')}-${part('day')}T00:00:00Z`)
 return {start:new Date(sourceDay-28*86400000).toISOString().slice(0,10),end:new Date(sourceDay-86400000).toISOString().slice(0,10)}
}
