/** 界面文案映射：库内码 → 中文（页面不直接露英文码） */
export const 表类型 = { water: '水表', electric: '电表', 水表: '水表', 电表: '电表' }
export const 表状态 = { active: '在用', inactive: '停用', 在用: '在用', 停用: '停用' }
export const 账期状态 = { open: '开放', sealed: '已封印', expired: '已作废', 开放: '开放', 已封印: '已封印' }
export const 批状态 = { open: '开放', sealed: '已封印', expired: '已作废' }
export const 命运 = {
  pending: '未决',
  accept_short: '认短',
  accept_over: '认溢',
  quarantine: '隔离',
}
export const 回执状态 = {
  success: '整批成功',
  partial: '部分成功',
  failed: '整批失败（已回滚）',
  rejected: '整批拒绝（账期封印）',
}
export const 装载架构 = {
  atomic: '整批原子（任一坏行整批回滚）',
  split: '分落（成功行落库，坏行进隔离）',
}
export const 失败原因 = {
  METER_NOT_FOUND: '表具不存在',
  PERIOD_NOT_FOUND: '账期不存在',
  PERIOD_SEALED: '账期已封印',
  INVALID_QTY: '数量非法',
  EMPTY_DOC_NO: '单号为空',
  DUPLICATE_IN_BATCH: '批内重复',
  DUPLICATE_IN_TABLE: '正式表已存在',
  INVALID_TIMESTAMP: '发生时刻非法',
  WRONG_KIND: '读数类型未知',
}

export function 标(map, key) {
  if (key == null || key === '') return '—'
  return map[key] || String(key)
}
