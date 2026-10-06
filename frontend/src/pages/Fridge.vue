<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页 · 拆后仍点母行 · 每行可发起在架分装</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>
          {{ label[L] }}
          <span class="subtotal">合计 {{ subtotal(L) }}</span>
        </h3>
        <span v-for="x in by(L)" :key="x.id" class="lot">
          {{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}
          <em v-if="x.split_from" class="child-tag">子批↑{{ x.split_from }}</em>
          <SplitForm :lot="x" @done="load" />
        </span>
      </section>
    </div>
    <p class="muted">全层总计 {{ total }}（各层小计之和）</p>
    <button style="margin-top:12px" @click="sweep">过期下架</button>
  </div>
</template>
<script setup>
import { ref, onMounted, computed } from 'vue'
import { api } from '../api'
import SplitForm from '../components/SplitForm.vue'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
function by(L) { return rows.value.filter(r => r.layer === L) }
function subtotal(L) { return by(L).reduce((s, r) => s + (Number(r.qty_remain) || 0), 0) }
const total = computed(() => rows.value.reduce((s, r) => s + (Number(r.qty_remain) || 0), 0))
async function load() { rows.value = await api('/fridge') }
async function sweep() { await api('/expire-sweep', { method: 'POST', body: '{}' }); await load() }
onMounted(load)
</script>
