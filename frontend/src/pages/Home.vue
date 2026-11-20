<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 批状态, 标, 装载架构 } from '../labels'
const data = ref(null)
const err = ref('')
onMounted(async () => {
  try { data.value = await api.overview() } catch (e) { err.value = e.message }
})
</script>
<template>
  <div>
    <h1>总览</h1>
    <p class="sub">当前账期估抄、实抄、对齐与封印状态</p>
    <p v-if="err" class="msg err">{{ err }}</p>
    <div class="grid" v-if="data">
      <div class="stat">当前账期<b>{{ data.current_period }}</b></div>
      <div class="stat">装载架构<b>{{ 标(装载架构, data.load_mode) }}</b></div>
      <div class="stat">开放账期<b>{{ data.open_periods }}</b></div>
      <div class="stat">已封印账期<b>{{ data.sealed_periods }}</b></div>
      <div class="stat">表具数<b>{{ data.meter_count }}</b></div>
      <div class="stat">批量成功<b>{{ data.bulk_success_painted ?? 0 }}</b></div>
      <div class="stat">未决差额<b>{{ data.pending_variances }}</b></div>
      <div class="stat">估抄正式笔数<b>{{ data.estimate_count }}</b></div>
      <div class="stat">实抄正式笔数<b>{{ data.actual_count }}</b></div>
      <div class="stat">隔离区笔数<b>{{ data.quarantine_count }}</b></div>
    </div>
    <div class="panel" v-if="data">
      <h3>笔数对账</h3>
      <p class="sub">
        回执成功累计 {{ data.receipt_success_total }} 笔、失败累计 {{ data.receipt_fail_total }} 笔；
        正式表（估 {{ data.estimate_count }} + 实 {{ data.actual_count }}）只含回执成功行，
        隔离区 {{ data.quarantine_count }} 笔只含分落架构的失败行，两者互不重叠。
      </p>
    </div>
    <div class="panel" v-if="data">
      <h3>最近对账批</h3>
      <table>
        <thead><tr><th>批号</th><th>账期</th><th>匹配</th><th>差额</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="b in data.recent_batches" :key="b.batch_id">
            <td>{{ b.batch_id }}</td>
            <td>{{ b.period_id }}</td>
            <td>{{ b.matched_count }}</td>
            <td>{{ b.variance_count }}</td>
            <td><span class="tag" :class="b.status">{{ 标(批状态, b.status) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
