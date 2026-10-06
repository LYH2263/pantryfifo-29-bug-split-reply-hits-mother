<template>
  <div>
    <h1>{{ props.layer }} 层 · 子批独立成行</h1>
    <p class="muted">本层合计 {{ total }}（与全层页该层小计同源同和）</p>
    <span v-for="x in rows" :key="x.id" class="lot">
      {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}
      <em v-if="x.split_from" class="child-tag">子批↑{{ x.split_from }}</em>
      <SplitForm :lot="x" @done="load" />
    </span>
  </div>
</template>
<script setup>
import { ref, watch, onMounted, computed } from 'vue'
import { api } from '../api'
import SplitForm from '../components/SplitForm.vue'
const props = defineProps({ layer: String })
const rows = ref([])
const total = computed(() => rows.value.reduce((s, r) => s + (Number(r.qty_remain) || 0), 0))
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
watch(() => props.layer, load)
onMounted(load)
</script>
