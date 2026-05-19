<script setup lang="ts">
import { ref } from 'vue'
import { postRecall, type RecallResponse } from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

const userId = useAdminUserId()

const query = ref('')
const topK = ref(5)
const voice = ref(false)
const includeKg = ref<'auto' | 'on' | 'off'>('auto')
const includeSensitiveKg = ref(false)

const result = ref<RecallResponse | null>(null)
const elapsedMs = ref<number | null>(null)
const err = ref('')
const loading = ref(false)

async function run() {
  err.value = ''
  loading.value = true
  result.value = null
  elapsedMs.value = null
  const t0 = performance.now()
  try {
    const body: Parameters<typeof postRecall>[1] = {
      query: query.value.trim(),
      top_k: topK.value,
      voice: voice.value,
      include_sensitive_kg: includeSensitiveKg.value,
    }
    if (includeKg.value !== 'auto') {
      body.include_kg = includeKg.value === 'on'
    }
    result.value = await postRecall(userId.value, body)
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    elapsedMs.value = Math.round(performance.now() - t0)
    loading.value = false
  }
}
</script>

<template>
  <section class="panel recall-page">
    <header class="panel-head">
      <div>
        <h2>召回调试（vector + KG 融合）</h2>
        <p class="muted small">
          走 MCP <code>eidolon_memory_recall_context</code>。voice=true 时使用
          LiveKit 50ms KG 超时,non-voice 走 1s 较宽窗口。
        </p>
      </div>
    </header>

    <form class="recall-form" @submit.prevent="run">
      <label class="block">
        查询
        <textarea v-model="query" rows="2" placeholder="例：self likes tea / 妈妈最近怎么样" />
      </label>
      <div class="row gap">
        <label>top_k
          <input type="number" min="1" max="50" v-model.number="topK" />
        </label>
        <label class="check"><input type="checkbox" v-model="voice" /> voice (LiveKit hot path)</label>
        <label>include_kg
          <select v-model="includeKg">
            <option value="auto">auto (settings)</option>
            <option value="on">on</option>
            <option value="off">off</option>
          </select>
        </label>
        <label class="check"><input type="checkbox" v-model="includeSensitiveKg" /> sensitive (health)</label>
        <button type="submit" :disabled="loading || !query.trim()">{{ loading ? '召回中…' : '召回' }}</button>
      </div>
    </form>

    <p v-if="err" class="error">{{ err }}</p>
    <p v-if="elapsedMs !== null" class="muted small">
      端到端 {{ elapsedMs }} ms · vector {{ result?.records.length ?? 0 }} · KG {{ result?.kg_triples.length ?? 0 }}
    </p>

    <div v-if="result" class="recall-grid">
      <article class="recall-context">
        <h3>合成 context（喂给 LLM 的字符串）</h3>
        <pre>{{ result.context || '(空)' }}</pre>
      </article>

      <article>
        <h3>KG 三元组</h3>
        <table v-if="result.kg_triples.length" class="records">
          <thead>
            <tr><th>主语</th><th>谓词</th><th>宾语</th><th>valid_from</th><th>valid_to</th></tr>
          </thead>
          <tbody>
            <tr v-for="(t, i) in result.kg_triples" :key="t.id ?? `kg-${i}`" :class="{ ended: !!t.valid_to }">
              <td><code>{{ t.subject }}</code></td>
              <td>{{ t.predicate }}</td>
              <td><code>{{ t.object }}</code></td>
              <td class="mono small">{{ t.valid_from ?? '—' }}</td>
              <td class="mono small">{{ t.valid_to ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted small">无 KG 命中（query 无规范实体或 KG 为空）</p>
      </article>

      <article>
        <h3>向量片段（top {{ result.records.length }}）</h3>
        <div v-if="result.records.length" class="hit-list">
          <div v-for="(r, i) in result.records" :key="`v-${i}`" class="hit">
            <div class="row gap small muted">
              <span>{{ String((r.metadata as Record<string, unknown>)?.wing ?? '—') }}</span>
              <span>·</span>
              <span>{{ String((r.metadata as Record<string, unknown>)?.room ?? '—') }}</span>
              <span v-if="(r.metadata as Record<string, unknown>)?.similarity">
                · sim {{ Number((r.metadata as Record<string, unknown>).similarity).toFixed(3) }}
              </span>
            </div>
            <div class="hit-body">{{ String((r as Record<string, unknown>).value ?? '') }}</div>
          </div>
        </div>
        <p v-else class="muted small">无向量命中</p>
      </article>
    </div>
  </section>
</template>
