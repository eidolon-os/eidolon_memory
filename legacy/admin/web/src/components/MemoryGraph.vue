<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { GraphEdge, GraphNode, KnowledgeGraphSnapshot, PalaceGraphSnapshot } from '../api/client'
import { fetchKnowledgeGraph, fetchPalaceGraph } from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

type GraphKind = 'knowledge' | 'palace'

const userId = useAdminUserId()
const kind = ref<GraphKind>('knowledge')
const entityFilter = ref('')
const currentOnly = ref(true)
const kg = ref<KnowledgeGraphSnapshot | null>(null)
const palace = ref<PalaceGraphSnapshot | null>(null)
const err = ref('')
const loading = ref(false)

type LayoutNode = GraphNode & { x: number; y: number }

function layoutCircle(nodes: GraphNode[], width: number, height: number): LayoutNode[] {
  if (!nodes.length) return []
  const cx = width / 2
  const cy = height / 2
  const r = Math.min(width, height) * 0.38
  return nodes.map((n, i) => {
    const angle = (2 * Math.PI * i) / nodes.length - Math.PI / 2
    return {
      ...n,
      x: cx + r * Math.cos(angle),
      y: cy + r * Math.sin(angle),
    }
  })
}

const activeSnapshot = computed(() => (kind.value === 'knowledge' ? kg.value : palace.value))

const layout = computed(() => {
  const snap = activeSnapshot.value
  if (!snap?.nodes?.length) {
    return { nodes: [] as LayoutNode[], edges: [] as GraphEdge[], pos: new Map<string, LayoutNode>() }
  }
  const nodes = layoutCircle(snap.nodes, 720, 420)
  const pos = new Map(nodes.map((n) => [n.id, n]))
  const edges = (snap.edges ?? []).filter((e) => pos.has(e.source) && pos.has(e.target))
  return { nodes, edges, pos }
})

const edgeLines = computed(() => {
  const { edges, pos } = layout.value
  if (!pos) return []
  return edges.map((e) => {
    const a = pos.get(e.source)!
    const b = pos.get(e.target)!
    return { ...e, x1: a.x, y1: a.y, x2: b.x, y2: b.y, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 }
  })
})

async function load() {
  err.value = ''
  loading.value = true
  try {
    if (kind.value === 'knowledge') {
      const p = new URLSearchParams({
        current_only: String(currentOnly.value),
        max_triples: '400',
      })
      const ent = entityFilter.value.trim()
      if (ent) p.set('entity', ent)
      kg.value = await fetchKnowledgeGraph(userId.value, p)
      palace.value = null
    } else {
      palace.value = await fetchPalaceGraph(userId.value)
      kg.value = null
    }
  } catch (e) {
    kg.value = null
    palace.value = null
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch([userId, kind, currentOnly], load, { immediate: true })

function nodeFill(n: GraphNode): string {
  if (n.kind === 'room') return n.is_tunnel ? '#f59e0b' : '#3b82f6'
  const t = (n.entity_type || '').toLowerCase()
  if (t === 'person') return '#8b5cf6'
  if (t === 'project') return '#10b981'
  return '#64748b'
}

function shortLabel(label: string, max = 14): string {
  return label.length > max ? label.slice(0, max - 1) + '…' : label
}
</script>

<template>
  <section class="panel graph-page">
    <h2>关系图</h2>
    <p class="muted">
      MemPalace 支持两类图：<strong>知识图谱</strong>（实体三元组，存于宫殿目录
      <code>knowledge_graph.sqlite3</code>）与<strong>宫殿拓扑图</strong>（跨翼 tunnel room，由 Chroma 元数据构建）。
    </p>

    <div class="row gap knobs">
      <label>
        类型
        <select v-model="kind">
          <option value="knowledge">知识图谱 (KG)</option>
          <option value="palace">宫殿拓扑</option>
        </select>
      </label>
      <template v-if="kind === 'knowledge'">
        <label>实体过滤<input v-model="entityFilter" placeholder="可空" @keyup.enter="load" /></label>
        <label><input v-model="currentOnly" type="checkbox" /> 仅当前有效事实</label>
      </template>
      <button type="button" :disabled="loading" @click="load">刷新</button>
    </div>

    <p v-if="loading" class="muted">加载图数据…</p>
    <p v-if="err" class="error">{{ err }}</p>

    <template v-if="activeSnapshot && !loading">
      <div v-if="!activeSnapshot.available" class="muted empty-graph">
        {{ activeSnapshot.reason || '暂无图数据' }}
        <span v-if="kind === 'knowledge'">
          — 写入事实后可经 steward / <code>mempalace_kg_add</code> 填充。
        </span>
      </div>

      <template v-else>
        <div class="graph-stats mono muted" v-if="kind === 'knowledge' && kg?.stats">
          实体 {{ kg.stats.entities }} · 三元组 {{ kg.stats.triples }} · 当前
          {{ kg.stats.current_facts }} · 类型
          {{ (kg.stats.relationship_types as string[])?.slice(0, 8).join(', ') }}
          <span v-if="kg.capped" class="warn">（已截断）</span>
        </div>
        <div v-else-if="kind === 'palace' && palace?.stats" class="graph-stats mono muted">
          房间 {{ palace.stats.total_rooms }} · tunnel {{ palace.stats.tunnel_rooms }} · 边
          {{ palace.stats.total_edges }}
          <span v-if="palace.total_rooms"> · 展示 {{ palace.nodes.length }}/{{ palace.total_rooms }}</span>
          <span v-if="palace.capped" class="warn">（已截断）</span>
        </div>

        <div v-if="layout.nodes.length" class="graph-canvas-wrap">
          <svg class="graph-canvas" viewBox="0 0 720 420" role="img" aria-label="关系图">
            <line
              v-for="e in edgeLines"
              :key="e.id"
              :x1="e.x1"
              :y1="e.y1"
              :x2="e.x2"
              :y2="e.y2"
              class="graph-edge"
            />
            <text
              v-for="e in edgeLines"
              :key="e.id + '-lbl'"
              :x="e.mx"
              :y="e.my"
              class="graph-edge-label"
              text-anchor="middle"
            >
              {{ shortLabel(e.label, 12) }}
            </text>
            <g v-for="n in layout.nodes" :key="n.id">
              <circle :cx="n.x" :cy="n.y" r="10" :fill="nodeFill(n)" class="graph-node-dot" />
              <text :x="n.x" :y="n.y + 22" class="graph-node-label" text-anchor="middle">
                {{ shortLabel(n.label) }}
              </text>
            </g>
          </svg>
        </div>

        <table class="records graph-table">
          <thead>
            <tr>
              <th>源</th>
              <th>关系</th>
              <th>目标</th>
              <th v-if="kind === 'knowledge'">时效</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in activeSnapshot.edges.slice(0, 80)" :key="e.id">
              <td class="mono">{{ e.source }}</td>
              <td>{{ e.label }}</td>
              <td class="mono">{{ e.target }}</td>
              <td v-if="kind === 'knowledge'" class="muted">
                {{ e.current ? '当前' : e.valid_to || '—' }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="activeSnapshot.edges.length > 80" class="muted">
          表格仅显示前 80 条边，共 {{ activeSnapshot.edges.length }} 条。
        </p>
      </template>
    </template>
  </section>
</template>
