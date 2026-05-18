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
    const res = await postMemory({
      wing: wing.value.trim(),
      room: room.value.trim(),
      text: text.value.trim(),
      metadata,
    })
    ok.value = res.detail || '已投递（202 Accepted）'
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
    <h2>写入记忆（JetStream → Worker）</h2>
    <p class="muted">
      通过 JetStream 投递 <code>ConversationTurnPayload</code>，由
      <code>eidolon-memory-worker</code> 消费后落盘；列表/搜索走 MCP HTTP（需
      <code>deploy/dev/run_all.sh</code>）。需本机 NATS 可达且 worker 在跑；非
      <code>noop</code> steward 时正文可能被提炼而非原样存储。
    </p>
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
