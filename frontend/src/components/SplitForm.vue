<template>
  <span class="split-inline">
    <button class="mini" v-if="!open" @click="openForm">分装</button>
    <span v-else class="split-box" @click.stop>
      <input type="number" v-model.number="qty" min="0" :max="lot.qty_remain" step="any" />
      <button class="mini" @click="doPreview">预览</button>
      <button class="mini primary" :disabled="!previewOk" @click="doConfirm">确认分装</button>
      <button class="mini" @click="cancel">取消</button>
      <span v-if="p && p.ok" class="split-preview">
        母批 {{ p.before }} → {{ p.parent_after }}，新子批 +{{ p.child_qty }}
        <template v-if="p.conserved">（合计 {{ p.sum_after }}，守恒 ✓）</template>
      </span>
      <span v-else-if="err" class="split-err">{{ err }}</span>
    </span>
  </span>
</template>
<script setup>
import { ref, computed } from 'vue'
import { api } from '../api'

const props = defineProps({ lot: Object })
const emit = defineEmits(['done'])
const open = ref(false)
const qty = ref(null)
const p = ref(null)
const err = ref('')
const previewOk = computed(() => p.value && p.value.ok)

function openForm() {
  open.value = true
  qty.value = null
  p.value = null
  err.value = ''
}
function cancel() { open.value = false; p.value = null; err.value = '' }

async function doPreview() {
  err.value = ''; p.value = null
  try {
    // 预览只做计算：后端不写库、母批余量不变
    p.value = await api('/splits/preview', {
      method: 'POST', body: JSON.stringify({ lot_id: props.lot.id, qty: qty.value }),
    })
  } catch (e) { err.value = e.message }
}
async function doConfirm() {
  err.value = ''
  try {
    // 确认前重新预览一次，确认与提交使用同一校验结果
    p.value = await api('/splits/preview', {
      method: 'POST', body: JSON.stringify({ lot_id: props.lot.id, qty: qty.value }),
    })
    await api('/splits/confirm', {
      method: 'POST', body: JSON.stringify({ lot_id: props.lot.id, qty: qty.value }),
    })
    open.value = false; p.value = null
    emit('done')
  } catch (e) { err.value = e.message }
}
</script>
