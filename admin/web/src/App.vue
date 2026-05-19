<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import KnowledgeGraph from './components/KnowledgeGraph.vue'
import MemoryGraph from './components/MemoryGraph.vue'
import MemoryHierarchy from './components/MemoryHierarchy.vue'
import MemoryList from './components/MemoryList.vue'
import MemorySearch from './components/MemorySearch.vue'
import MemoryWrite from './components/MemoryWrite.vue'
import RecallDebug from './components/RecallDebug.vue'
import type { HealthResponse } from './api/client'
import { fetchHealth } from './api/client'
import { provideAdminUser } from './composables/useAdminUser'

type Tab = 'list' | 'search' | 'write' | 'hierarchy' | 'graph' | 'kg' | 'recall'

interface TabSpec {
  key: Tab
  label: string
  hint: string
}

const TABS: TabSpec[] = [
  { key: 'list', label: '列表', hint: '宫殿/翼/房/抽屉浏览' },
  { key: 'search', label: '语义搜索', hint: 'vector 召回（不含 KG）' },
  { key: 'recall', label: '召回调试', hint: 'vector + KG 融合 (LiveKit 同源)' },
  { key: 'kg', label: '知识图谱', hint: 'bi-temporal 事实（NATS 写）' },
  { key: 'hierarchy', label: '层级', hint: 'wing → room → drawer' },
  { key: 'graph', label: '关系图', hint: '宫殿 / KG 可视化' },
  { key: 'write', label: '写入对话', hint: '投递 ConversationTurn' },
]

const tab = ref<Tab>('list')
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

const activeUser = computed(() =>
  health.value?.users.find((u) => u.user_id === selectedUserId.value),
)
const activeTabHint = computed(() => TABS.find((t) => t.key === tab.value)?.hint ?? '')
</script>

<template>
  <header class="topbar">
    <div class="topbar-row">
      <h1>Eidolon 记忆 Admin</h1>
      <button type="button" class="btn-ghost" :disabled="!health && !healthErr" @click="loadHealth">
        刷新健康
      </button>
    </div>

    <div v-if="health" class="health-strip">
      <label class="user-select">
        <span class="lbl">用户</span>
        <select v-model="selectedUserId">
          <option v-for="u in health.users" :key="u.user_id" :value="u.user_id">
            {{ u.user_id }} :{{ u.port }} {{ u.agent_reachable ? '✓' : '✗' }}
          </option>
        </select>
      </label>
      <span class="pill steward">steward · {{ health.steward_mode }}</span>
      <span v-if="activeUser" :class="['pill', activeUser.agent_reachable ? 'live' : 'dead']">
        {{ activeUser.agent_reachable ? 'agent online' : 'agent unreachable' }}
      </span>
      <span v-if="activeUser" class="mono path">
        palace <code>{{ activeUser.palace_path }}</code>
      </span>
      <span v-if="activeUser" class="mono path">
        MCP <code>{{ activeUser.mcp_http_url }}</code>
      </span>
    </div>
    <div v-else-if="healthErr" class="health-strip error">
      ⚠ {{ healthErr }} ｜ 先启动 supervisor / agent + Admin API
    </div>

    <nav class="tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        :class="{ active: tab === t.key }"
        type="button"
        :title="t.hint"
        @click="tab = t.key"
      >
        {{ t.label }}
      </button>
    </nav>
    <p class="tab-hint muted">{{ activeTabHint }}</p>
  </header>

  <main v-if="selectedUserId" class="shell">
    <MemoryList v-if="tab === 'list'" />
    <MemorySearch v-else-if="tab === 'search'" />
    <RecallDebug v-else-if="tab === 'recall'" />
    <KnowledgeGraph v-else-if="tab === 'kg'" />
    <MemoryHierarchy v-else-if="tab === 'hierarchy'" />
    <MemoryGraph v-else-if="tab === 'graph'" />
    <MemoryWrite v-else-if="tab === 'write'" />
  </main>
  <main v-else class="shell muted empty-state">
    <p>等待健康检查…</p>
    <p class="small">如长时间无响应,确认 <code>eidolon-memory-supervisor</code> 与 admin API 都在跑。</p>
  </main>
</template>
