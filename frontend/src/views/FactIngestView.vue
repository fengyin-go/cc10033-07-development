<template>
  <section class="page" data-module="fact_ingest">
    <header class="page-head">
      <div>
        <h2>事实入仓</h2>
        <p class="page-desc">维度自然键映射到正式代理键，事实按映射写入分区；维度键迟到时先待匹配，回填后金额不翻倍。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="ingest-refresh" type="button" @click="reloadAll">刷新视图</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">自然键映射</span>
        <strong class="stat-value" data-testid="stat-nk-count">{{ naturalKeys.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">分区记录</span>
        <strong class="stat-value" data-testid="stat-record-count">{{ summary.record_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待匹配</span>
        <strong class="stat-value" data-testid="stat-pending-count">{{ summary.pending_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已关联</span>
        <strong class="stat-value" data-testid="stat-matched-count">{{ summary.matched_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">金额合计</span>
        <strong class="stat-value" data-testid="stat-amount">{{ summary.measure_totals['金额'] ?? 0 }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text" data-testid="ingest-error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="ingest-notice">{{ notice }}</p>

    <!-- 1. 按映射写入事实分区 -->
    <form class="analysis-form" data-testid="write-form" @submit.prevent="submitWrite">
      <label>
        <span>事实表</span>
        <select v-model.number="writeForm.factId" data-testid="write-fact">
          <option v-for="fact in facts" :key="fact.id" :value="fact.id">{{ fact['事实名称'] }}</option>
        </select>
      </label>
      <label>
        <span>分区字段</span>
        <input v-model="writeForm.partitionField" data-testid="write-partition-field" placeholder="dt" />
      </label>
      <label>
        <span>来源系统</span>
        <input v-model="writeForm.source" data-testid="write-source" placeholder="pos" />
      </label>
    </form>
    <label class="create-field">
      <span>事实记录（JSON 数组，含来源记录ID与自然键）</span>
      <textarea
        v-model="writeForm.records"
        class="code-input"
        data-testid="write-records"
        rows="5"
        spellcheck="false"
      />
    </label>
    <div class="page-actions">
      <button class="btn primary" data-testid="write-submit" type="button" @click="submitWrite">按映射写入分区</button>
    </div>

    <!-- 2. 待匹配检索 -->
    <form class="filter-bar" data-testid="pending-filter" @submit.prevent="reloadPending">
      <label class="filter-item">
        <span>事实名称</span>
        <input v-model="pendingFilters.fact_name" data-testid="filter-fact-name" placeholder="按事实名称检索" />
      </label>
      <label class="filter-item">
        <span>来源记录ID</span>
        <input v-model="pendingFilters.source_record_id" data-testid="filter-source-record" placeholder="按来源记录检索" />
      </label>
      <label class="filter-item">
        <span>分区值</span>
        <input v-model="pendingFilters.partition_value" data-testid="filter-partition" placeholder="按分区值检索" />
      </label>
      <label class="filter-item">
        <span>维度编码</span>
        <input v-model="pendingFilters.dim_code" data-testid="filter-dim-code" placeholder="DIM_PRODUCT" />
      </label>
      <label class="filter-item">
        <span>自然键</span>
        <input v-model="pendingFilters.natural_key" data-testid="filter-natural-key" placeholder="P-100" />
      </label>
      <button class="btn" data-testid="pending-query" type="submit">查询待匹配</button>
      <button class="btn ghost" data-testid="pending-reset" type="button" @click="resetPending">重置条件</button>
    </form>

    <table class="data-table" data-testid="pending-table">
      <thead>
        <tr>
          <th>来源记录ID</th>
          <th>来源系统</th>
          <th>分区</th>
          <th>粒度</th>
          <th>维度 / 自然键</th>
          <th>代理键</th>
          <th>金额</th>
          <th>匹配状态</th>
          <th>回填标记</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in partitionRows"
          :key="row.id"
          :data-testid="`fact-row-${row['来源记录ID']}`"
          :data-status="row.status"
        >
          <td>{{ row['来源记录ID'] }}</td>
          <td>{{ row['来源系统'] }}</td>
          <td>{{ row['分区'] }}</td>
          <td>{{ row['粒度声明'] }}</td>
          <td>{{ row['维度编码'] }} / {{ row['自然键'] }}</td>
          <td>
            <span v-if="row['代理键']">{{ row['代理键'] }}</span>
            <span v-else class="muted">待分配</span>
          </td>
          <td>{{ row['金额'] }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row['回填标记'] }}</td>
        </tr>
        <tr v-if="!partitionRows.length">
          <td colspan="9" class="empty-state">暂无符合条件的事实记录</td>
        </tr>
      </tbody>
    </table>

    <!-- 3. 维度自然键登记与迟到回填 -->
    <div class="analysis-summary">
      <div class="analysis-card">
        <h3>登记维度自然键</h3>
        <form class="analysis-form" data-testid="nk-form" @submit.prevent="submitNaturalKey">
          <label>
            <span>维度编码</span>
            <input v-model="nkForm.dimCode" data-testid="nk-dim-code" placeholder="DIM_PRODUCT" />
          </label>
          <label>
            <span>自然键</span>
            <input v-model="nkForm.naturalKey" data-testid="nk-value" placeholder="P-100" />
          </label>
          <label>
            <span>来源系统</span>
            <input v-model="nkForm.source" data-testid="nk-source" placeholder="product_master" />
          </label>
          <button class="btn primary" data-testid="nk-submit" type="submit">确定代理键并回填</button>
        </form>
        <p class="page-desc">同一自然键重复登记保持原代理键；不同自然键不会占用同一个代理键。</p>
      </div>
      <div class="analysis-card">
        <h3>按源记录定位</h3>
        <form class="analysis-form" @submit.prevent="lookupSource">
          <label>
            <span>来源记录ID</span>
            <input v-model="sourceQuery" data-testid="source-query" placeholder="SO-001" />
          </label>
          <button class="btn" data-testid="source-submit" type="submit">检索</button>
        </form>
        <p v-if="sourceResult" class="page-desc" data-testid="source-result">
          共 {{ sourceResult.total }} 条，金额合计 {{ sourceResult.amount }}
        </p>
      </div>
    </div>

    <table class="data-table" data-testid="natural-key-table">
      <thead>
        <tr>
          <th>目标维度</th>
          <th>自然键</th>
          <th>正式代理键</th>
          <th>键值定义</th>
          <th>来源系统</th>
          <th>待匹配事实</th>
          <th>登记时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in naturalKeys" :key="row.id" :data-testid="`nk-row-${row.自然键}`">
          <td>{{ row.目标维度 }}</td>
          <td>{{ row.自然键 }}</td>
          <td>{{ row.代理键 }}</td>
          <td>{{ row.键值名称 || '—' }}</td>
          <td>{{ row.来源系统 || '—' }}</td>
          <td>{{ row.待匹配事实数 }}</td>
          <td>{{ row.登记时间 }}</td>
        </tr>
        <tr v-if="!naturalKeys.length">
          <td colspan="7" class="empty-state">尚未登记自然键映射</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>待匹配事实 {{ pending.total }} 条，记录总数 {{ summary.record_count }} 条</span>
      <span v-if="lastWrite" data-testid="write-result">
        本次写入 {{ lastWrite.written }} 条（待匹配 {{ lastWrite.pending }} 条，重复 {{ lastWrite.duplicates }} 条）
      </span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import {
  bindNaturalKey,
  findFactBySource,
  listNaturalKeys,
  listPendingMatches,
  writePartition,
} from '@/api/ingest'
import type {
  FactPartitionRow,
  NaturalKeyRow,
  PartitionWriteResult,
  PendingMatchResult,
} from '@/api/ingest'

const SAMPLE_RECORDS = JSON.stringify(
  [
    {
      来源记录ID: 'SO-001',
      来源系统: 'pos',
      dt: '2026-11-01',
      product_nk: 'P-100',
      store_nk: 'S-001',
      sales_amt: 1280.5,
      sales_qty: 3,
    },
    {
      来源记录ID: 'SO-002',
      来源系统: 'pos',
      dt: '2026-11-01',
      product_nk: 'P-200',
      store_nk: 'S-001',
      sales_amt: 640,
      sales_qty: 1,
    },
  ],
  null,
  2,
)

const facts = ref<EntryRow[]>([])
const naturalKeys = ref<NaturalKeyRow[]>([])
const pending = ref<PendingMatchResult>({
  items: [],
  total: 0,
  status: '待匹配',
  summary: {
    record_count: 0,
    pending_count: 0,
    matched_count: 0,
    measure_totals: {},
    partitions: {},
  },
})
const allRows = ref<FactPartitionRow[]>([])
const lastWrite = ref<PartitionWriteResult | null>(null)
const sourceResult = ref<{ total: number; amount: number } | null>(null)
const sourceQuery = ref('SO-001')
const errorMessage = ref('')
const notice = ref('')

const writeForm = reactive({
  factId: 0,
  partitionField: 'dt',
  source: 'pos',
  records: SAMPLE_RECORDS,
})
const nkForm = reactive({ dimCode: 'DIM_PRODUCT', naturalKey: '', source: 'product_master' })
const pendingFilters = reactive<Record<string, string>>({
  fact_name: '',
  source_record_id: '',
  partition_value: '',
  dim_code: '',
  natural_key: '',
})

const currentFactName = computed(
  () => facts.value.find((fact) => Number(fact.id) === writeForm.factId)?.['事实名称'] as string ?? '',
)
const summary = computed(() => pending.value.summary)
// 筛选条件为空时展示全部事实分区；有筛选条件时展示后端命中的记录。
const partitionRows = computed(() => {
  const filtered = Object.values(pendingFilters).some((value) => value.trim())
  return filtered ? pending.value.items : allRows.value
})

function fail(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
  notice.value = ''
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    const factPage = await listEntries('fact_design', {})
    facts.value = factPage.items
    if (!writeForm.factId && facts.value.length) {
      writeForm.factId = Number(facts.value[0].id)
    }
    naturalKeys.value = await listNaturalKeys()
    allRows.value = (await listPendingMatches({}, true)).items
    await reloadPending()
  } catch (error) {
    fail(error, '事实入仓数据读取失败')
  }
}

async function reloadPending() {
  const filters = Object.fromEntries(
    Object.entries(pendingFilters).filter(([, value]) => value.trim()),
  )
  if (!filters.fact_name && currentFactName.value) {
    filters.fact_name = currentFactName.value
  }
  const includeMatched = Object.values(pendingFilters).some((value) => value.trim())
  errorMessage.value = ''
  try {
    pending.value = await listPendingMatches(filters, includeMatched)
    allRows.value = pending.value.items
  } catch (error) {
    fail(error, '待匹配事实读取失败')
  }
}

function resetPending() {
  for (const key of Object.keys(pendingFilters)) pendingFilters[key] = ''
  void reloadAll()
}

async function submitWrite() {
  errorMessage.value = ''
  notice.value = ''
  let records: Record<string, unknown>[]
  try {
    records = JSON.parse(writeForm.records)
    if (!Array.isArray(records) || !records.length) throw new Error('记录必须是非空数组')
  } catch (error) {
    fail(error instanceof Error ? new Error(`记录 JSON 解析失败：${error.message}`) : null, '记录格式错误')
    return
  }
  if (!writeForm.factId) {
    errorMessage.value = '请先选择事实表'
    return
  }
  try {
    lastWrite.value = await writePartition(writeForm.factId, {
      records,
      partition_field: writeForm.partitionField || undefined,
      source: writeForm.source || undefined,
    })
    notice.value = `已写入 ${lastWrite.value.written} 条，待匹配 ${lastWrite.value.pending} 条，重复导入 ${lastWrite.value.duplicates} 条`
    await reloadAll()
  } catch (error) {
    fail(error, '事实分区写入失败')
  }
}

async function submitNaturalKey() {
  errorMessage.value = ''
  notice.value = ''
  const naturalKey = nkForm.naturalKey.trim()
  if (!nkForm.dimCode.trim() || !naturalKey) {
    errorMessage.value = '请填写维度编码和自然键'
    return
  }
  try {
    const result = await bindNaturalKey({
      dim_code: nkForm.dimCode.trim(),
      natural_key: naturalKey,
      source: nkForm.source.trim(),
      auto_backfill: true,
    })
    notice.value = result.created
      ? `${nkForm.dimCode}/${naturalKey} 已分配代理键 ${result.代理键}，回填 ${result.backfilled_matches} 条事实`
      : `${nkForm.dimCode}/${naturalKey} 已存在，沿用代理键 ${result.代理键}`
    await reloadAll()
    await lookupSource()
  } catch (error) {
    fail(error, '自然键登记失败')
  }
}

async function lookupSource() {
  if (!sourceQuery.value.trim()) return
  errorMessage.value = ''
  try {
    const result = await findFactBySource(sourceQuery.value.trim())
    sourceResult.value = { total: result.total, amount: result.amount }
  } catch (error) {
    fail(error, '源记录检索失败')
  }
}

onMounted(reloadAll)
</script>
