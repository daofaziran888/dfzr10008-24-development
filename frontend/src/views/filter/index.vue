<template>
  <section class="page" data-module="filter">
    <header class="page-head">
      <div>
        <h2>数据筛选引擎管理</h2>
        <p class="page-desc">
          维护筛选条件、排序与分页配置；可多选模板一次提交，按目标数据源冻结口径生成可追溯的结果集。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记筛选模板</button>
        <button class="btn" type="button" @click="reloadBatches">刷新执行批次</button>
        <button class="btn" type="button" @click="exportRows">导出数据筛选引擎清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="batch-bar" data-testid="batch-bar">
      <div class="batch-head">
        <strong>批量执行</strong>
        <span class="hint">已选 {{ selected.length }} 个模板，多条结果整组返回并逐条说明</span>
      </div>
      <div class="batch-controls">
        <label class="filter-item">
          <span>目标数据源</span>
          <select v-model="targetSource">
            <option v-for="source in dataSources" :key="source.数据源" :value="source.数据源">
              {{ source.数据源 }}（版本 {{ source.版本 }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>中断模拟（调试）</span>
          <select v-model="stopAfter">
            <option value="">不中断</option>
            <option v-for="code in selected" :key="code" :value="code">执行到 {{ code }} 后中断</option>
          </select>
        </label>
        <button class="btn primary" type="button" :disabled="!selected.length || running" @click="executeBatch">
          {{ running ? '执行中…' : '执行所选模板' }}
        </button>
        <button class="btn ghost" type="button" @click="selected = []">清空选择</button>
      </div>
      <p v-if="batchMessage" class="batch-message" :class="{ 'error-text': batchFailed }">{{ batchMessage }}</p>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th class="check-cell">
            <input type="checkbox" :checked="allSelected" :disabled="!rows.length" @change="toggleAll" />
          </th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td class="check-cell">
            <input
              type="checkbox"
              :checked="selected.includes(String(row['模板编号']))"
              :value="String(row['模板编号'])"
              @change="toggleSelect(String(row['模板编号']))"
            />
          </td>
          <td v-for="column in columns" :key="column">{{ displayCell(row[column]) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据筛选引擎数据，可先登记筛选模板</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据筛选引擎记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="currentBatch" class="panel" data-testid="batch-result">
      <div class="panel-head">
        <div>
          <strong>批次 {{ currentBatch.批次编号 }}</strong>
          <span class="tag" :class="batchTagClass(currentBatch.状态)">{{ currentBatch.状态 }}</span>
          <span class="hint">
            成功 {{ currentBatch.汇总.成功 }} · 失败 {{ currentBatch.汇总.失败 }} · 未执行 {{ currentBatch.汇总.未执行 }}
            · 本次{{ currentBatch.本次执行 }}
          </span>
        </div>
        <div class="row-actions">
          <button
            v-if="currentBatch.状态 !== '已完成'"
            class="btn"
            type="button"
            @click="resumeBatch(currentBatch.批次编号)"
          >
            从失败模板继续
          </button>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>顺序</th>
            <th>模板编号</th>
            <th>模板名称</th>
            <th>目标数据源</th>
            <th>状态</th>
            <th>说明</th>
            <th>结果集</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in currentBatch.结果项" :key="item.模板编号">
            <td>{{ item.顺序 }}</td>
            <td>{{ item.模板编号 }}</td>
            <td>{{ item.模板名称 ?? '—' }}</td>
            <td>{{ item.目标数据源 }}</td>
            <td><span class="tag" :class="itemTagClass(item.状态)">{{ item.状态 }}</span></td>
            <td :class="{ 'error-text': item.状态 !== '成功' }">{{ item.说明 }}</td>
            <td>{{ item.结果集编号 ?? '—' }}</td>
            <td class="row-actions">
              <button v-if="item.结果集编号" class="link" type="button" @click="openResult(item.结果集编号)">
                查看结果集
              </button>
              <button
                v-if="item.状态 !== '成功'"
                class="link"
                type="button"
                @click="retryItem(currentBatch!.批次编号, item.模板编号)"
              >
                单独重跑
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="resultDetail" class="panel" data-testid="result-set">
      <div class="panel-head">
        <div>
          <strong>结果集 {{ resultDetail.结果集编号 }}</strong>
          <span class="hint">
            共 {{ resultDetail.总行数 }} 行，当前展示 {{ resultDetail.行数 }} 行 · 冻结于 {{ resultDetail.冻结时间 }}
          </span>
        </div>
        <div class="row-actions">
          <button class="btn" type="button" @click="openAnalysis(resultDetail.结果集编号)">下游分析入口</button>
          <button class="btn ghost" type="button" @click="closePanels">收起</button>
        </div>
      </div>
      <div class="frozen-grid">
        <div>
          <span class="stat-label">冻结筛选条件</span>
          <p>{{ resultDetail.冻结条件.筛选条件 || '—' }}</p>
          <ul class="condition-list">
            <li v-for="(condition, index) in resultDetail.冻结条件.筛选条件明细" :key="index">
              {{ condition.字段 }} {{ condition.运算符 }} {{ condition.值 }}
            </li>
          </ul>
        </div>
        <div>
          <span class="stat-label">展示顺序</span>
          <p>{{ resultDetail.展示顺序.join(' → ') }}</p>
          <span class="stat-label">排序 / 分页</span>
          <p>
            {{ resultDetail.冻结条件.排序规则?.字段 || '未指定' }}
            {{ resultDetail.冻结条件.排序规则?.方向 || '' }} ·
            每页 {{ resultDetail.冻结条件.分页规则?.每页 || '—' }} 条 / 第
            {{ resultDetail.冻结条件.分页规则?.页码 || 1 }} 页
          </p>
          <span class="stat-label">追溯信息</span>
          <p class="hint">
            数据源版本 {{ resultDetail.追溯信息.数据源版本 }} · 快照
            {{ resultDetail.追溯信息.数据源快照时间 }} · 条件摘要
            {{ resultDetail.追溯信息.条件摘要.slice(0, 12) }}…
          </p>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="field in resultDetail.展示顺序" :key="field">{{ field }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in resultDetail.行" :key="index">
            <td v-for="field in resultDetail.展示顺序" :key="field">{{ displayCell(row[field]) }}</td>
          </tr>
          <tr v-if="!resultDetail.行.length">
            <td :colspan="resultDetail.展示顺序.length" class="empty-state">冻结条件下没有命中数据</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="downstream" class="panel downstream" data-testid="analysis-entry">
      <div class="panel-head">
        <div>
          <strong>下游分析入口 · {{ downstream.结果集编号 }}</strong>
          <span class="tag" :class="consistent ? 'tag-ok' : 'tag-bad'">
            {{ consistent ? '条件与展示顺序和页面一致' : '与页面不一致' }}
          </span>
        </div>
        <button class="btn ghost" type="button" @click="downstream = null">收起</button>
      </div>
      <p class="hint">
        来源批次 {{ downstream.来源批次 }} · 模板 {{ downstream.模板编号 }} ·
        {{ downstream.口径来源 }} · 共 {{ downstream.行数 }} / {{ downstream.总行数 }} 行
      </p>      <div class="frozen-grid">
        <div>
          <span class="stat-label">筛选条件</span>
          <p>{{ downstream.筛选条件 }}</p>
          <ul class="condition-list">
            <li v-for="(condition, index) in downstream.筛选条件明细" :key="index">
              {{ condition.字段 }} {{ condition.运算符 }} {{ condition.值 }}
            </li>
          </ul>
        </div>
        <div>
          <span class="stat-label">展示顺序</span>
          <p>{{ downstream.展示顺序.join(' → ') }}</p>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="field in downstream.展示顺序" :key="field">{{ field }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in downstream.行" :key="index">
            <td v-for="field in downstream.展示顺序" :key="field">{{ displayCell(row[field]) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" data-testid="batch-history">
      <div class="panel-head">
        <strong>执行批次</strong>
        <span class="hint">同一批参数重复提交会复用原结果集，不会重复写入</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>目标数据源</th>
            <th>模板</th>
            <th>状态</th>
            <th>成功 / 失败 / 未执行</th>
            <th>提交时间</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch.批次编号">
            <td>{{ batch.批次编号 }}</td>
            <td>{{ batch.目标数据源 }}</td>
            <td>{{ batch.模板编号列表.join('、') }}</td>
            <td><span class="tag" :class="batchTagClass(batch.状态)">{{ batch.状态 }}</span></td>
            <td>{{ batch.汇总.成功 }} / {{ batch.汇总.失败 }} / {{ batch.汇总.未执行 }}</td>
            <td>{{ batch.提交时间 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="currentBatch = batch">查看</button>
              <button v-if="batch.状态 !== '已完成'" class="link" type="button" @click="resumeBatch(batch.批次编号)">
                继续执行
              </button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="7" class="empty-state">暂无执行批次，请先多选模板并执行</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'

type Cell = string | number | null | undefined
type Row = Record<string, unknown>

interface Condition {
  字段: string
  运算符: string
  值: string
}

interface BatchItem {
  顺序: number
  模板编号: string
  模板名称: string | null
  目标数据源: string
  状态: string
  ok: boolean
  说明: string
  结果集编号: string | null
  行数: number
}

interface Batch {
  批次编号: string
  目标数据源: string
  模板编号列表: string[]
  状态: string
  提交时间: string
  汇总: { 成功: number; 失败: number; 未执行: number }
  结果项: BatchItem[]
  本次执行: string
}

interface ResultSet {
  结果集编号: string
  冻结时间: string
  行数: number
  总行数: number
  展示顺序: string[]
  行: Record<string, Cell>[]
  冻结条件: {
    筛选条件: string
    筛选条件明细: Condition[]
    排序规则: { 字段: string; 方向: string }
    分页规则: { 每页: number; 页码: number }
  }
  追溯信息: { 数据源版本: string; 数据源快照时间: string; 条件摘要: string }
}

interface AnalysisEntry {
  结果集编号: string
  来源批次: string
  模板编号: string
  口径来源: string
  筛选条件: string
  筛选条件明细: Condition[]
  展示顺序: string[]
  行数: number
  总行数: number
  行: Record<string, Cell>[]
}

const ENDPOINT = '/api/filter'
const columns = ["模板编号", "模板名称", "目标数据源", "筛选条件", "排序字段", "分页配置", "结果格式", "模板状态"]
const actions = ["启用模板", "暂停模板", "废弃模板"]
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const batchMessage = ref('')
const batchFailed = ref(false)
const running = ref(false)
const filters = ref<Record<string, string>>({})
const selected = ref<string[]>([])
const dataSources = ref<{ 数据源: string; 版本: string; 字段: string[] }[]>([])
const targetSource = ref('订单表')
const stopAfter = ref('')
const currentBatch = ref<Batch | null>(null)
const batches = ref<Batch[]>([])
const resultDetail = ref<ResultSet | null>(null)
const downstream = ref<AnalysisEntry | null>(null)

const stats = computed(() => [
  { label: '筛选模板总数', value: rows.value.length },
  { label: '已启用模板', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '已暂停模板', value: rows.value.filter((row) => row.status === '已暂停').length },
  { label: '执行结果集', value: batches.value.reduce((sum, batch) => sum + batch.汇总.成功, 0) },
])

const allSelected = computed(
  () => rows.value.length > 0 && rows.value.every((row) => selected.value.includes(String(row['模板编号']))),
)

const consistent = computed(() => {
  if (!resultDetail.value || !downstream.value) return false
  return (
    JSON.stringify(resultDetail.value.展示顺序) === JSON.stringify(downstream.value.展示顺序) &&
    JSON.stringify(resultDetail.value.冻结条件.筛选条件明细) ===
      JSON.stringify(downstream.value.筛选条件明细)
  )
})

function displayCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join('、')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function batchTagClass(status: string): string {
  if (status === '已完成') return 'tag-ok'
  if (status === '已中断') return 'tag-warn'
  return 'tag-bad'
}

function itemTagClass(status: string): string {
  if (status === '成功') return 'tag-ok'
  if (status === '未执行') return 'tag-warn'
  return 'tag-bad'
}

function toggleSelect(code: string) {
  selected.value = selected.value.includes(code)
    ? selected.value.filter((item) => item !== code)
    : [...selected.value, code]
}

function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selected.value = checked ? rows.value.map((row) => String(row['模板编号'])) : []
}

// 中断模拟只能选当前已勾选的模板；取消勾选后自动回落到“不中断”。
watch(selected, (codes) => {
  if (stopAfter.value && !codes.includes(stopAfter.value)) {
    stopAfter.value = ''
  }
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '筛选模板登记入口尚未接入审批流'
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(payload?.detail ?? fallback)
  }
  return (await response.json()) as T
}

async function executeBatch() {
  errorMessage.value = ''
  batchMessage.value = ''
  batchFailed.value = false
  if (!selected.value.length) {
    batchFailed.value = true
    batchMessage.value = '请先勾选至少一个筛选模板'
    return
  }
  running.value = true
  try {
    const response = await request(`${ENDPOINT}/batches`, {
      method: 'POST',
      body: JSON.stringify({
        templates: selected.value,
        target_source: targetSource.value,
        stop_after: stopAfter.value || null,
      }),
    })
    const payload = await readJson<{ ok: boolean; message: string; batch: Batch }>(
      response,
      '批量执行失败，请稍后重试',
    )
    currentBatch.value = payload.batch
    batchMessage.value = payload.message
    batchFailed.value = payload.batch.状态 === '已中断'
    await reload()
    await reloadBatches()
  } catch (error) {
    batchFailed.value = true
    batchMessage.value = error instanceof Error ? error.message : '批量执行失败'
  } finally {
    running.value = false
  }
}

async function resumeBatch(batchNo: string) {
  batchMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/batches/${batchNo}/resume`, { method: 'POST' })
    const payload = await readJson<{ ok: boolean; message: string; batch: Batch }>(
      response,
      '继续执行失败，请稍后重试',
    )
    currentBatch.value = payload.batch
    batchMessage.value = payload.message
    batchFailed.value = !payload.ok
    await reload()
    await reloadBatches()
  } catch (error) {
    batchFailed.value = true
    batchMessage.value = error instanceof Error ? error.message : '继续执行失败'
  }
}

async function retryItem(batchNo: string, code: string) {
  batchMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/batches/${batchNo}/items/${code}/retry`, { method: 'POST' })
    const payload = await readJson<{ ok: boolean; message: string; batch: Batch }>(
      response,
      '单独重跑失败，请稍后重试',
    )
    currentBatch.value = payload.batch
    batchMessage.value = payload.message
    batchFailed.value = !payload.ok
    await reload()
    await reloadBatches()
  } catch (error) {
    batchFailed.value = true
    batchMessage.value = error instanceof Error ? error.message : '单独重跑失败'
  }
}

async function openResult(resultNo: string) {
  errorMessage.value = ''
  downstream.value = null
  try {
    const response = await request(`${ENDPOINT}/results/${resultNo}`)
    resultDetail.value = await readJson<ResultSet>(response, '结果集读取失败')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果集读取失败'
  }
}

function closePanels() {
  resultDetail.value = null
  downstream.value = null
}

async function openAnalysis(resultNo: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/results/${resultNo}/analysis`)
    downstream.value = await readJson<AnalysisEntry>(response, '下游分析入口读取失败')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下游分析入口读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据筛选引擎动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据筛选引擎操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('筛选模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据筛选引擎列表读取失败'
  }
}

async function reloadBatches() {
  try {
    const response = await request(`${ENDPOINT}/batches?size=50`)
    const payload = await readJson<{ items: Batch[] }>(response, '执行批次读取失败')
    batches.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行批次读取失败'
  }
}

async function loadDataSources() {
  try {
    const response = await request(`${ENDPOINT}/data-sources`)
    dataSources.value = await readJson<{ 数据源: string; 版本: string; 字段: string[] }[]>(
      response,
      '目标数据源读取失败',
    )
    if (dataSources.value.length && !dataSources.value.some((item) => item.数据源 === targetSource.value)) {
      targetSource.value = dataSources.value[0].数据源
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '目标数据源读取失败'
  }
}

onMounted(async () => {
  await Promise.all([loadDataSources(), reloadBatches()])
  await reload()
})
</script>

<style scoped>
.batch-bar {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.batch-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
}
.batch-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
}
.batch-controls select {
  min-width: 200px;
  padding: 5px 8px;
}
.batch-message {
  margin: 8px 0 0;
  font-size: 13px;
  color: #1f6feb;
}
.panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-top: 16px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}
.downstream {
  border-color: #1f6feb;
}
.frozen-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
  margin-bottom: 10px;
}
.frozen-grid p {
  margin: 4px 0;
  font-size: 13px;
}
.condition-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--muted);
}
.check-cell {
  width: 32px;
  text-align: center;
}
.hint {
  color: var(--muted);
  font-size: 12px;
}
.tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.tag-ok {
  background: #e7f6ec;
  color: #1a7f37;
}
.tag-bad {
  background: #fde8e8;
  color: #b42318;
}
.tag-warn {
  background: #fff4e5;
  color: #b25e09;
}
</style>
