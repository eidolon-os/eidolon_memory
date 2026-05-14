<script setup lang="ts">
import { onMounted, ref } from 'vue'
import MemoryHierarchy from './components/MemoryHierarchy.vue'
import MemoryList from './components/MemoryList.vue'
import MemorySearch from './components/MemorySearch.vue'
import MemoryWrite from './components/MemoryWrite.vue'
import type { HealthResponse } from './api/client'
import { fetchHealth } from './api/client'

const tab = ref<'list' | 'search' | 'write' | 'hierarchy'>('list')
const health = ref<HealthResponse | null>(null)
const healthErr = ref('')

onMounted(async () => {
  try {
    health.value = await fetchHealth()
    healthErr.value = ''
  } catch (e) {
    health.value = null
    healthErr.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<template>
  <header class="topbar">
    <h1>Eidolon 记忆 Admin</h1>
    <div v-if="health" class="health ok">
       palace:
      <span class="mono">{{ health.palace_path }}</span>
      • steward {{ health.steward_mode }}
    </div>
    <div v-else-if="healthErr" class="health error">{{ healthErr }}（可先启动后端 <code>:8010</code>）</div>
    <nav class="tabs">
      <button :class="{ active: tab === 'list' }" type="button" @click="tab = 'list'">列表</button>
      <button :class="{ active: tab === 'search' }" type="button" @click="tab = 'search'">搜索</button>
      <button :class="{ active: tab === 'write' }" type="button" @click="tab = 'write'">写入</button>
      <button :class="{ active: tab === 'hierarchy' }" type="button" @click="tab = 'hierarchy'">层级</button>
    </nav>
  </header>
  <main class="shell">
    <MemoryList v-if="tab === 'list'" />
    <MemorySearch v-else-if="tab === 'search'" />
    <MemoryWrite v-else-if="tab === 'write'" />
    <MemoryHierarchy v-else />
  </main>
</template>
