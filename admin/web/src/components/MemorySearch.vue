<script setup lang="ts">
import { ref } from 'vue'
import type { MemoryRecord } from '../api/client'
import { fetchMemorySearch } from '../api/client'

const query = ref('')
const userId = ref('default')
const topK = ref(8)
const wing = ref('')
const room = ref('')
const records = ref<MemoryRecord[]>([])
const err = ref('')
const loading = ref(false)

async function run() {
  err.value = ''
  loading.value = true
  records.value = []
  try {
    const p = new URLSearchParams({
      query: query.value.trim(),
      user_id: userId.value,
      top_k: String(topK.value),
    })
    if (wing.value.trim()) p.set('wing', wing.value.trim())
    if (room.value.trim()) p.set('room', room.value.trim())
    const data = await fetchMemorySearch(p)
    records.value = data.records ?? []
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h2>语义搜索（与 MCP 一致）</h2>
    <div class="grid">
      <label>query<input v-model="query" /></label>
      <label>user_id<input v-model="userId" /></label>
      <label>top_k<input v-model.number="topK" type="number" min="1" max="100" /></label>
      <label>wing (可选)<input v-model="wing" /></label>
      <label>room (可选)<input v-model="room" /></label>
    </div>
    <button type="button" :disabled="loading || !query.trim()" @click="run">搜索</button>
    <p v-if="loading" class="muted">检索中…</p>
    <p v-if="err" class="error">{{ err }}</p>
    <article v-for="r in records" :key="r.key" class="hit">
      <header class="mono">{{ r.key }}</header>
      <pre>{{ JSON.stringify(r, null, 2) }}</pre>
    </article>
    <p v-if="!loading && records.length === 0 && query.trim()" class="muted">没有命中</p>
  </section>
</template>
