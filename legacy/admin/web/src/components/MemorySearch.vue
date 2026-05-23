<script setup lang="ts">
import { ref, watch } from 'vue'
import type { MemoryRecord } from '../api/client'
import { fetchMemorySearch, recordSimilarity } from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

const userId = useAdminUserId()
const query = ref('')
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
      top_k: String(topK.value),
    })
    if (wing.value.trim()) p.set('wing', wing.value.trim())
    if (room.value.trim()) p.set('room', room.value.trim())
    const data = await fetchMemorySearch(userId.value, p)
    const list = data.records ?? []
    list.sort((a, b) => {
      const sa = recordSimilarity(a)
      const sb = recordSimilarity(b)
      if (sa == null && sb == null) return 0
      if (sa == null) return 1
      if (sb == null) return -1
      return sb - sa
    })
    records.value = list
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(userId, () => {
  if (query.value.trim()) void run()
})
</script>

<template>
  <section class="panel">
    <h2>语义搜索（MCP）</h2>
    <p class="muted">
      用户 <code>{{ userId }}</code> · 结果按 <code>metadata.similarity</code> 降序（越大越相关）。
    </p>
    <div class="grid">
      <label>query<input v-model="query" /></label>
      <label>top_k<input v-model.number="topK" type="number" min="1" max="100" /></label>
      <label>wing (可选)<input v-model="wing" /></label>
      <label>room (可选)<input v-model="room" /></label>
    </div>
    <button type="button" :disabled="loading || !query.trim()" @click="run">搜索</button>
    <p v-if="loading" class="muted">检索中…</p>
    <p v-if="err" class="error">{{ err }}</p>
    <article v-for="r in records" :key="r.key" class="hit">
      <header>
        <span class="mono">{{ r.key }}</span>
        <span v-if="recordSimilarity(r) != null" class="sim">
          similarity {{ recordSimilarity(r)!.toFixed(4) }}
        </span>
      </header>
      <p>{{ typeof r.value === 'string' ? r.value : JSON.stringify(r.value) }}</p>
      <p class="muted mono">
        wing {{ r.metadata?.wing ?? '—' }} · room {{ r.metadata?.room ?? '—' }}
      </p>
    </article>
    <p v-if="!loading && records.length === 0 && query.trim()" class="muted">没有命中</p>
  </section>
</template>
