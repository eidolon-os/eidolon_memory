<script setup lang="ts">
import { ref } from 'vue'
import { postMemory } from '../api/client'

const wing = ref('Wing_Profile')
const room = ref('profile_core')
const text = ref('')
const metaJson = ref('')
const ok = ref('')
const err = ref('')
const loading = ref(false)

async function submit() {
  ok.value = ''
  err.value = ''
  let metadata: Record<string, unknown> | undefined
  const raw = metaJson.value.trim()
  if (raw) {
    try {
      metadata = JSON.parse(raw) as Record<string, unknown>
    } catch {
      err.value = 'metadata JSON 无效'
      return
    }
  }
  loading.value = true
  try {
    await postMemory({
      wing: wing.value.trim(),
      room: room.value.trim(),
      text: text.value.trim(),
      metadata,
    })
    ok.value = '写入成功（若内容已存在则可能去重跳过）'
    text.value = ''
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h2>写入记忆（ingest_fragment）</h2>
    <p class="muted">等价于 NATS <code>MEMORY_STORE</code>：指定 <code>wing</code> / <code>room</code> / 正文，可选额外 metadata。</p>
    <div class="grid">
      <label>wing<input v-model="wing" /></label>
      <label>room<input v-model="room" /></label>
    </div>
    <label class="block">正文<textarea v-model="text" rows="5"></textarea></label>
    <label class="block">metadata（JSON，可选）<textarea v-model="metaJson" rows="3" placeholder="{&quot;user_id&quot;: &quot;alice&quot;}"></textarea></label>
    <button type="button" :disabled="loading || !wing.trim() || !room.trim() || !text.trim()" @click="submit">
      提交
    </button>
    <p v-if="ok" class="ok">{{ ok }}</p>
    <p v-if="err" class="error">{{ err }}</p>
  </section>
</template>
