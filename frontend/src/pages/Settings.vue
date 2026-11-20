<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { 装载架构 } from '../labels'
const current = ref('')
const loadMode = ref('atomic')
const msg = ref('')
const err = ref('')
async function load() {
  const s = await api.settings()
  current.value = s.current_period || ''
  loadMode.value = s.load_mode || 'atomic'
}
async function savePeriod() {
  err.value = ''; msg.value = ''
  try {
    await api.putSetting('current_period', current.value)
    msg.value = '已保存当前账期'
    await load()
  } catch (e) { err.value = e.message }
}
async function saveMode() {
  err.value = ''; msg.value = ''
  try {
    await api.putSetting('load_mode', loadMode.value)
    msg.value = '已切换全局装载架构（仅对切换后的新批次生效）'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div>
    <h1>设置</h1>
    <p class="sub">运行参数</p>
    <div class="panel">
      <div class="row">
        <label>当前账期<input v-model="current" /></label>
        <button @click="savePeriod">保存</button>
      </div>
    </div>
    <div class="panel">
      <h3>批量装载架构（全局唯一口径）</h3>
      <p class="sub">
        atomic：任一行失败整批回滚，正式表零写入；split：合法行落正式表，非法行进隔离区。
        切换后同一坏文件可在「隔离区与回执」页对照两种结果。
      </p>
      <div class="row">
        <label>架构
          <select v-model="loadMode">
            <option value="atomic">{{ 装载架构.atomic }}</option>
            <option value="split">{{ 装载架构.split }}</option>
          </select>
        </label>
        <button @click="saveMode">切换架构</button>
      </div>
      <p class="msg ok" v-if="msg">{{ msg }}</p>
      <p class="msg err" v-if="err">{{ err }}</p>
    </div>
  </div>
</template>
