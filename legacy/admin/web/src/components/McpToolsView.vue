<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { fetchMcpTools, type McpTool } from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

const userId = useAdminUserId()

const tools = ref<McpTool[]>([])
const filter = ref('')
const loading = ref(false)
const err = ref('')
const expanded = ref<Set<string>>(new Set())

async function reload() {
  if (!userId.value) return
  loading.value = true
  err.value = ''
  try {
    const res = await fetchMcpTools(userId.value)
    tools.value = res.tools
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

onMounted(reload)
watch(userId, () => reload())

function toggle(name: string) {
  if (expanded.value.has(name)) expanded.value.delete(name)
  else expanded.value.add(name)
  expanded.value = new Set(expanded.value)
}

function expandAll() {
  expanded.value = new Set(tools.value.map((t) => t.name))
}

function collapseAll() {
  expanded.value = new Set()
}

function matches(t: McpTool): boolean {
  const q = filter.value.trim().toLowerCase()
  if (!q) return true
  return (
    t.name.toLowerCase().includes(q) ||
    t.description.toLowerCase().includes(q)
  )
}

function group(name: string): string {
  if (name.startsWith('eidolon_memory_kg_')) return 'KG (knowledge graph)'
  if (name.startsWith('eidolon_memory_')) return 'memory'
  return 'other'
}
</script>

<template>
  <section class="panel mcp-page">
    <header class="panel-head">
      <div>
        <h2>MCP 工具清单</h2>
        <p class="muted small">
          来自 agent_runner @ <code>{{ userId }}</code> 的 control-plane MCP — 调用
          <code>session.list_tools()</code>。LiveKit / Admin / Claude IDE 客户端
          看到的就是这套。
        </p>
      </div>
      <div class="row gap">
        <button type="button" @click="expandAll">展开全部</button>
        <button type="button" @click="collapseAll">折叠全部</button>
        <button type="button" :disabled="loading" @click="reload">
          {{ loading ? '加载…' : '刷新' }}
        </button>
      </div>
    </header>

    <div class="row gap mcp-filter">
      <label class="grow">
        过滤
        <input v-model="filter" placeholder="按 name / description 搜索" />
      </label>
      <span class="muted small">{{ tools.length }} 个工具</span>
    </div>

    <p v-if="err" class="error">{{ err }}</p>

    <ul class="mcp-list">
      <li
        v-for="t in tools.filter(matches)"
        :key="t.name"
        :class="['mcp-card', { open: expanded.has(t.name) }]"
      >
        <header class="mcp-card-head" @click="toggle(t.name)">
          <div class="mcp-head-left">
            <span :class="['tag', group(t.name) === 'KG (knowledge graph)' ? 'warn' : 'ok']">
              {{ group(t.name) }}
            </span>
            <code class="mcp-name">{{ t.name }}</code>
          </div>
          <span class="muted small">{{ expanded.has(t.name) ? '▾' : '▸' }}</span>
        </header>
        <p class="mcp-desc">{{ t.description || '(无描述)' }}</p>
        <div v-if="expanded.has(t.name)" class="mcp-schema">
          <h4>inputSchema</h4>
          <pre>{{ JSON.stringify(t.input_schema, null, 2) }}</pre>
        </div>
      </li>
      <li v-if="!tools.filter(matches).length && !loading" class="muted">无匹配工具</li>
    </ul>
  </section>
</template>
