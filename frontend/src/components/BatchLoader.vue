<script setup>
import { ref } from 'vue'
import { 标, 回执状态, 失败原因 } from '../labels'

const props = defineProps({
  kind: { type: String, required: true }, // estimate | actual
  example: { type: String, required: true },
  postBatch: { type: Function, required: true },
})
const emit = defineEmits(['loaded'])
const text = ref('')
const receipt = ref(null)
const parseErr = ref('')
const fileName = ref('')

function onFile(e) {
  const f = e.target.files[0]
  if (!f) return
  fileName.value = f.name
  const reader = new FileReader()
  reader.onload = () => { text.value = String(reader.result || '') }
  reader.readAsText(f)
}

async function submit() {
  receipt.value = null
  parseErr.value = ''
  let rows
  try {
    rows = JSON.parse(text.value)
  } catch (e) {
    parseErr.value = 'JSON 解析失败：' + e.message
    return
  }
  if (!Array.isArray(rows) || !rows.length) {
    parseErr.value = '内容必须是非空 JSON 数组，每行一笔读数'
    return
  }
  try {
    receipt.value = await props.postBatch({ rows })
    emit('loaded')
  } catch (e) {
    parseErr.value = e.message
  }
}
</script>

<template>
  <div class="panel">
    <h3>批量装载（{{ kind === 'estimate' ? '估抄' : '实抄' }}）</h3>
    <p class="sub">粘贴 JSON 数组或选择文件，一次装多笔；架构口径由「设置 → 装载架构」全局决定</p>
    <textarea v-model="text" rows="7" :placeholder="example" spellcheck="false"></textarea>
    <div class="row">
      <input type="file" accept=".json,.txt" @change="onFile" />
      <span v-if="fileName" class="sub">已选：{{ fileName }}</span>
      <button @click="submit">批量装载</button>
    </div>
    <p class="msg err" v-if="parseErr">{{ parseErr }}</p>

    <div v-if="receipt" class="receipt">
      <p :class="['msg', receipt.status === 'success' ? 'ok' : 'err']">
        回执 {{ receipt.receipt_id }}：{{ 标(回执状态, receipt.status) }}
        ｜架构 {{ receipt.load_mode }}
        ｜共 {{ receipt.total_count }} 笔，成功 {{ receipt.success_count }}，
        失败 {{ receipt.fail_count }}<template v-if="receipt.quarantined_count">
          ，入隔离 {{ receipt.quarantined_count }}</template>
      </p>
      <table v-if="receipt.failures && receipt.failures.length">
        <thead><tr><th>行号</th><th>表号</th><th>单号</th><th>原因码</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="f in receipt.failures" :key="f.row_index">
            <td>{{ f.row_index }}</td>
            <td>{{ f.meter_id || '—' }}</td>
            <td>{{ f.doc_no || '—' }}</td>
            <td>{{ f.reason_code }}</td>
            <td>{{ 标(失败原因, f.reason_code) || f.reason_text }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
