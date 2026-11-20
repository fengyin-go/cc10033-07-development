import { request } from './client'

export type FactPartitionRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  '事实名称': string
  '业务过程': string
  '粒度声明': string
  '分区字段': string
  '分区值': string
  '分区': string
  '来源系统': string
  '来源记录ID': string
  '维度编码': string
  '自然键': string
  代理键: number | null
  '待匹配维度': string[]
  外键: Record<string, { 自然键: string; 代理键: number | null; 状态: string }>
  金额: number
  匹配状态: string
  回填标记: string
  写入时间: string
  回填时间: string | null
  [field: string]: unknown
}

export type NaturalKeyRow = {
  id: number
  目标维度: string
  键值名称: string
  自然键: string
  代理键: number
  编号: number
  来源系统: string
  生成策略: string
  登记时间: string
  待匹配事实数: number
}

export type PendingMatchResult = {
  items: FactPartitionRow[]
  total: number
  status: string
  summary: {
    record_count: number
    pending_count: number
    matched_count: number
    measure_totals: Record<string, number>
    partitions: Record<string, number>
  }
}

export type PartitionWriteResult = {
  fact_name: string
  written: number
  inserted: number
  duplicates: number
  matched: number
  pending: number
  partitions: Record<string, number>
  measure_totals: Record<string, number>
  items: FactPartitionRow[]
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail ?? fallback)
  }
  return (await response.json()) as T
}

export async function listNaturalKeys(filters: Record<string, string> = {}): Promise<NaturalKeyRow[]> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/surrogate_key/natural-keys?${query}`)
  const payload = await readJson<{ items: NaturalKeyRow[] }>(response, '自然键映射读取失败')
  return payload.items
}

export async function bindNaturalKey(values: {
  dim_code: string
  natural_key: string
  source?: string
  auto_backfill?: boolean
}): Promise<{ 代理键: number; created: boolean; backfilled_matches: number }> {
  const response = await request('/api/surrogate_key/natural-keys', {
    method: 'POST',
    body: JSON.stringify(values),
  })
  return await readJson(response, '自然键登记失败')
}

export async function writePartition(
  factEntryId: number,
  values: {
    records: Record<string, unknown>[]
    partition_field?: string
    dimension_mappings?: Record<string, string>
    source?: string
  },
): Promise<PartitionWriteResult> {
  const response = await request(`/api/fact_design/${factEntryId}/partitions`, {
    method: 'POST',
    body: JSON.stringify(values),
  })
  return await readJson(response, '事实分区写入失败')
}

export async function listPendingMatches(
  filters: Record<string, string> = {},
  includeMatched = false,
): Promise<PendingMatchResult> {
  const params = { ...filters, include_matched: String(includeMatched) }
  const query = new URLSearchParams(params).toString()
  const response = await request(`/api/fact_design/pending-matches?${query}`)
  return await readJson(response, '待匹配事实读取失败')
}

export async function backfillFacts(values: {
  dim_code?: string
  natural_key?: string
  fact_name?: string
  source_record_id?: string
}): Promise<{ matched: number; remaining: number; measure_totals: Record<string, number> }> {
  const response = await request('/api/fact_design/backfill', {
    method: 'POST',
    body: JSON.stringify(values),
  })
  return await readJson(response, '回填失败')
}

export async function findFactBySource(sourceRecordId: string): Promise<{
  items: FactPartitionRow[]
  total: number
  amount: number
}> {
  const response = await request(`/api/fact_design/by-source/${encodeURIComponent(sourceRecordId)}`)
  return await readJson(response, '源记录检索失败')
}
