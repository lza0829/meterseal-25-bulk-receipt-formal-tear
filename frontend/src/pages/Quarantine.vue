<script setup>
import { onMounted, ref, computed } from 'vue'
import { api } from '../api'
import { 标, 回执状态, 失败原因, 装载架构 } from '../labels'

const periodId = ref('')
const qrows = ref([])
const receipts = ref([])
const overview = ref(null)

async function load() {
  qrows.value = await api.quarantine(periodId.value ? { period_id: periodId.value } : {})
  receipts.value = await api.receipts(periodId.value || undefined)
  overview.value = await api.overview()
}
const reasonSummary = computed(() => {
  const m = {}
  for (const q of qrows.value) m[q.reason_code] = (m[q.reason_code] || 0) + 1
  return Object.entries(m)
})
function parseCodes(s) {
  try { return JSON.parse(s || '{}') } catch { return {} }
}
onMounted(load)

</script>
<template>
  <div>
    <h1>隔离区与装载回执</h1>
    <p class="sub">隔离行只来自分落架构的行级校验失败；封印整批拒绝不进隔离</p>
    <div class="row">
      <label>账期筛选<input v-model="periodId" placeholder="留空看全部" /></label>
      <button class="secondary" @click="load">刷新</button>
    </div>

    <div class="grid" v-if="overview">
      <div class="stat">当前架构<b>{{ 标(装载架构, overview.load_mode) }}</b></div>
      <div class="stat">估抄正式笔数<b>{{ overview.estimate_count }}</b></div>
      <div class="stat">实抄正式笔数<b>{{ overview.actual_count }}</b></div>
      <div class="stat">隔离笔数<b>{{ overview.quarantine_count }}</b></div>
      <div class="stat">回执成功累计<b>{{ overview.receipt_success_total }}</b></div>
      <div class="stat">回执失败累计<b>{{ overview.receipt_fail_total }}</b></div>
    </div>

    <div class="panel">
      <h3>隔离区（{{ qrows.length }} 笔）</h3>
      <p class="sub" v-if="reasonSummary.length">
        原因码分布：
        <span v-for="[code, n] in reasonSummary" :key="code" class="tag expired">
          {{ 标(失败原因, code) }} × {{ n }}
        </span>
      </p>
      <table>
        <thead><tr><th>隔离号</th><th>回执</th><th>类型</th><th>账期</th><th>表号</th><th>单号</th><th>行号</th><th>原因码</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="q in qrows" :key="q.quarantine_id">
            <td>{{ q.quarantine_id }}</td>
            <td>{{ q.receipt_id }}</td>
            <td>{{ q.kind === 'estimate' ? '估抄' : '实抄' }}</td>
            <td>{{ q.period_id }}</td>
            <td>{{ q.meter_id || '—' }}</td>
            <td>{{ q.doc_no || '—' }}</td>
            <td>{{ q.row_index }}</td>
            <td>{{ q.reason_code }}</td>
            <td>{{ 标(失败原因, q.reason_code) || q.reason_text }}</td>
          </tr>
          <tr v-if="!qrows.length"><td colspan="9" class="sub">无隔离行</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>装载回执</h3>
      <table>
        <thead><tr><th>回执号</th><th>类型</th><th>账期</th><th>架构</th><th>状态</th><th>总数</th><th>成功</th><th>失败</th><th>原因码分布</th></tr></thead>
        <tbody>
          <tr v-for="r in receipts" :key="r.receipt_id">
            <td>{{ r.receipt_id }}</td>
            <td>{{ r.kind === 'estimate' ? '估抄' : '实抄' }}</td>
            <td>{{ r.period_id }}</td>
            <td>{{ 标(装载架构, r.load_mode) }}</td>
            <td><span class="tag" :class="r.status">{{ 标(回执状态, r.status) }}</span></td>
            <td>{{ r.total_count }}</td>
            <td>{{ r.success_count }}</td>
            <td>{{ r.fail_count }}</td>
            <td>{{ JSON.stringify(parseCodes(r.reason_codes)) }}</td>
          </tr>
          <tr v-if="!receipts.length"><td colspan="9" class="sub">无回执</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
