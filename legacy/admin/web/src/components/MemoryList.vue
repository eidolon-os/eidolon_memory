<script setup lang="ts">
import { ref, watch } from 'vue'
import type { MemoryRecord } from '../api/client'
import { fetchMemoryList } from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

const userId = useAdminUserId()
const limit = ref(100)
const offset = ref(0)
const includePrivate = ref(false)
const records = ref<MemoryRecord[]>([])
const hint = ref<number | null>(null)
const err = ref('')
const loading = ref(false)

async function load() {
  err.value = ''
  loading.value = true
  try {
    const p = new URLSearchParams({
      limit: String(limit.value),
      offset: String(offset.value),
      include_private: String(includePrivate.value),
    })
    const data = await fetchMemoryList(userId.value, p)
    records.value = data.records ?? []
    hint.value = data.total_hint ?? null
  } catch (e) {
    records.value = []
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch([userId, limit, offset, includePrivate], load, { immediate: true })

function shortText(v: unknown, n = 80): string {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s.length > n ? s.slice(0, n) + '…' : s
}

function prevPage() {
  offset.value = Math.max(0, offset.value - limit.value)
}

function nextPage() {
  if (records.value.length >= limit.value) {
    offset.value += limit.value
  }
}
</script>

<template>
  <section class="panel">
    <h2>记忆列表</h2>
    <p class="muted">
      通过当前用户 <code>{{ userId }}</code> 的 agent_runner MCP（<code>eidolon_memory_list</code>）分页列出宫殿抽屉；D1 控制面不提供删除。
    </p>
    <div class="row gap">
      <label>limit <input v-model.number="limit" type="number" min="1" /></label>
      <label><input v-model="includePrivate" type="checkbox" /> 包含隐私翼</label>
      <button type="button" :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="loading" class="muted">加载中…</p>
    <p v-if="err" class="error">{{ err }}</p>
    <div v-if="hint !== null && !loading" class="muted">本页 {{ records.length }} 条（分页提示 {{ hint }}）</div>
    <div class="pager">
      <button type="button" :disabled="offset === 0 || loading" @click="prevPage">上一页</button>
      <span class="muted">offset {{ offset }}</span>
      <button
        type="button"
        :disabled="records.length < limit || loading"
        @click="nextPage"
      >
        下一页
      </button>
    </div>
    <table class="records">
      <thead>
        <tr>
          <th>key</th>
          <th>正文</th>
          <th>wing</th>
          <th>room</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in records" :key="r.key">
          <td class="mono">{{ r.key }}</td>
          <td>{{ shortText(r.value) }}</td>
          <td class="mono">{{ r.metadata?.wing ?? '—' }}</td>
          <td class="mono">{{ r.metadata?.room ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!loading && records.length === 0" class="muted">无记录</p>
  </section>
</template>
