<script setup lang="ts">
import { onMounted, ref } from 'vue'
import MemoryHierarchy from './components/MemoryHierarchy.vue'
import MemoryList from './components/MemoryList.vue'
import MemorySearch from './components/MemorySearch.vue'
import MemoryWrite from './components/MemoryWrite.vue'
import type { HealthResponse } from './api/client'
import { fetchHealth } from './api/client'
import { provideAdminUser } from './composables/useAdminUser'

const tab = ref<'list' | 'search' | 'write' | 'hierarchy'>('list')
const health = ref<HealthResponse | null>(null)
const healthErr = ref('')
const selectedUserId = ref('')

provideAdminUser(selectedUserId)

async function loadHealth() {
  try {
    health.value = await fetchHealth()
    healthErr.value = ''
    if (!selectedUserId.value && health.value.users.length) {
      selectedUserId.value = health.value.default_user_id
    }
  } catch (e) {
    health.value = null
    healthErr.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(loadHealth)

const activeUser = () => health.value?.users.find((u) => u.user_id === selectedUserId.value)
</script>

<template>
  <header class="topbar">
    <h1>Eidolon 记忆 Admin</h1>
    <div v-if="health" class="health ok">
      <label>
        用户
        <select v-model="selectedUserId">
          <option v-for="u in health.users" :key="u.user_id" :value="u.user_id">
            {{ u.user_id }} :{{ u.port }}
            {{ u.agent_reachable ? '✓' : '✗' }}
          </option>
        </select>
      </label>
      <span v-if="activeUser()" class="mono">
        palace {{ activeUser()?.palace_path }}
        • MCP {{ activeUser()?.mcp_http_url }}
      </span>
      <span>steward {{ health.steward_mode }}</span>
    </div>
    <div v-else-if="healthErr" class="health error">
      {{ healthErr }}（先启动 supervisor/agent + Admin API）
    </div>
    <nav class="tabs">
      <button :class="{ active: tab === 'list' }" type="button" @click="tab = 'list'">列表</button>
      <button :class="{ active: tab === 'search' }" type="button" @click="tab = 'search'">搜索</button>
      <button :class="{ active: tab === 'write' }" type="button" @click="tab = 'write'">写入</button>
      <button :class="{ active: tab === 'hierarchy' }" type="button" @click="tab = 'hierarchy'">层级</button>
    </nav>
  </header>
  <main v-if="selectedUserId" class="shell">
    <MemoryList v-if="tab === 'list'" />
    <MemorySearch v-else-if="tab === 'search'" />
    <MemoryWrite v-else-if="tab === 'write'" />
    <MemoryHierarchy v-else />
  </main>
  <main v-else class="shell muted">选择用户或等待健康检查…</main>
</template>
