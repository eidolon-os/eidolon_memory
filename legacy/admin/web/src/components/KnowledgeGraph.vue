<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  fetchKgPredicates,
  fetchKgStats,
  fetchKgTimeline,
  postKgInvalidate,
  postKgTriple,
  type KgPredicates,
  type KgStats,
  type KgTripleOut,
} from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

const userId = useAdminUserId()

const stats = ref<KgStats | null>(null)
const predicates = ref<KgPredicates | null>(null)
const timeline = ref<KgTripleOut[]>([])

const loading = ref(false)
const err = ref('')
const filterEntity = ref('')
const limit = ref(60)
const includeSensitive = ref(false)

const writeOpen = ref(true)
const formSubject = ref('self')
const formPredicate = ref('likes')
const formObject = ref('')
const formConfidence = ref(0.95)
const formValidFrom = ref('')
const formValidTo = ref('')
const writeMsg = ref('')
const writeErr = ref('')
const writeBusy = ref(false)

const invSubject = ref('self')
const invPredicate = ref('likes')
const invObject = ref('')
const invMsg = ref('')
const invErr = ref('')
const invBusy = ref(false)

async function reload() {
  if (!userId.value) return
  loading.value = true
  err.value = ''
  try {
    const [s, p, t] = await Promise.all([
      fetchKgStats(userId.value),
      fetchKgPredicates(userId.value),
      fetchKgTimeline(userId.value, {
        entityName: filterEntity.value.trim() || undefined,
        limit: limit.value,
        includeSensitive: includeSensitive.value,
      }),
    ])
    stats.value = s
    predicates.value = p
    timeline.value = t.events
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch(userId, () => {
  if (userId.value) reload()
})
onMounted(reload)

const groupedPredicates = computed(() => {
  if (!predicates.value) return [] as Array<{ name: string; sensitive: boolean }>
  const sens = new Set(predicates.value.sensitive)
  return predicates.value.predicates.map((name) => ({ name, sensitive: sens.has(name) }))
})

async function submitWrite() {
  writeMsg.value = ''
  writeErr.value = ''
  writeBusy.value = true
  try {
    const res = await postKgTriple(userId.value, {
      subject: formSubject.value.trim(),
      predicate: formPredicate.value.trim(),
      object: formObject.value.trim(),
      confidence: formConfidence.value,
      valid_from: formValidFrom.value.trim() || undefined,
      valid_to: formValidTo.value.trim() || undefined,
    })
    writeMsg.value =
      res.status === 'applied'
        ? `已应用 (triple ${res.triple_id})`
        : `已发布,等待 worker 应用 (request ${res.request_id})`
    formObject.value = ''
    await reload()
  } catch (e) {
    writeErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    writeBusy.value = false
  }
}

async function submitInvalidate() {
  invMsg.value = ''
  invErr.value = ''
  invBusy.value = true
  try {
    const res = await postKgInvalidate(userId.value, {
      subject: invSubject.value.trim(),
      predicate: invPredicate.value.trim(),
      object: invObject.value.trim(),
    })
    invMsg.value =
      res.status === 'applied'
        ? '已 invalidate'
        : `已发布,等待 worker 应用 (request ${res.request_id})`
    invObject.value = ''
    await reload()
  } catch (e) {
    invErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    invBusy.value = false
  }
}

function predicateZh(p: string): string {
  const map: Record<string, string> = {
    likes: '喜欢', dislikes: '不喜欢', prefers: '偏好', does: '做',
    practices: '在练习', owns: '拥有', uses: '在使用',
    promised: '承诺', committed_to: '承诺要', planned_to: '计划',
    has_state: '处于状态', has_emotion: '感受', has_concern: '担心',
    worried_about: '担心', struggles_with: '困扰于',
    has_health_condition: '患有', takes_medication: '在服用', has_symptom: '有症状',
    attended: '参加了', experienced: '经历了', achieved: '达成了',
    child_of: '是…的孩子', parent_of: '是…的父母', partner_of: '是…的伴侣',
    sibling_of: '是…的兄弟姐妹', friend_of: '和…是朋友', colleague_of: '和…是同事',
    works_at: '在…工作', lives_in: '住在', studies_at: '在…学习',
    holds_role: '担任', born_in: '出生于',
  }
  return map[p] || p
}
</script>

<template>
  <section class="panel kg-page">
    <header class="panel-head">
      <div>
        <h2>知识图谱（KG）</h2>
        <p class="muted small">
          所有写入经 MCP <code>kg_add_triple</code> → NATS
          <code>agent.memory.cmd.{{ userId }}</code> → worker
          应用,JetStream 为唯一事实源。
        </p>
      </div>
      <button type="button" :disabled="loading" @click="reload">{{ loading ? '加载中…' : '刷新' }}</button>
    </header>

    <div class="kg-stats-strip">
      <div class="stat">
        <span class="stat-label">实体</span>
        <span class="stat-value">{{ stats?.entities ?? '—' }}</span>
      </div>
      <div class="stat">
        <span class="stat-label">三元组（全部）</span>
        <span class="stat-value">{{ stats?.triples_total ?? '—' }}</span>
      </div>
      <div class="stat ok">
        <span class="stat-label">当前生效</span>
        <span class="stat-value">{{ stats?.triples_active ?? '—' }}</span>
      </div>
      <div class="stat muted">
        <span class="stat-label">已失效</span>
        <span class="stat-value">{{ stats?.triples_invalidated ?? '—' }}</span>
      </div>
    </div>

    <p v-if="err" class="error">{{ err }}</p>

    <details :open="writeOpen" class="kg-write" @toggle="writeOpen = ($event.target as HTMLDetailsElement).open">
      <summary><strong>新建 / 失效 三元组</strong></summary>
      <div class="kg-write-grid">
        <form class="kg-form" @submit.prevent="submitWrite">
          <h3>新建</h3>
          <div class="row gap">
            <label>主语 <input v-model="formSubject" placeholder="self" /></label>
            <label>
              谓词
              <select v-model="formPredicate">
                <option v-for="p in groupedPredicates" :key="p.name" :value="p.name">
                  {{ p.name }}{{ p.sensitive ? ' ⚠' : '' }} — {{ predicateZh(p.name) }}
                </option>
              </select>
            </label>
            <label>宾语 <input v-model="formObject" placeholder="tea" /></label>
          </div>
          <div class="row gap">
            <label>置信度
              <input type="number" min="0" max="1" step="0.05" v-model.number="formConfidence" />
            </label>
            <label>valid_from <input v-model="formValidFrom" placeholder="2026-05-19T10:00:00Z" /></label>
            <label>valid_to <input v-model="formValidTo" placeholder="留空表示当前生效" /></label>
          </div>
          <button type="submit" :disabled="writeBusy || !formObject.trim()">
            {{ writeBusy ? '发布中…' : '发布' }}
          </button>
          <p v-if="writeMsg" class="ok small">{{ writeMsg }}</p>
          <p v-if="writeErr" class="error small">{{ writeErr }}</p>
        </form>

        <form class="kg-form" @submit.prevent="submitInvalidate">
          <h3>失效（结束某条事实）</h3>
          <div class="row gap">
            <label>主语 <input v-model="invSubject" /></label>
            <label>谓词 <input v-model="invPredicate" /></label>
            <label>宾语 <input v-model="invObject" placeholder="coffee" /></label>
          </div>
          <button type="submit" class="danger" :disabled="invBusy || !invObject.trim()">
            {{ invBusy ? '发布中…' : '失效' }}
          </button>
          <p v-if="invMsg" class="ok small">{{ invMsg }}</p>
          <p v-if="invErr" class="error small">{{ invErr }}</p>
        </form>
      </div>
    </details>

    <div class="kg-filters row gap">
      <label>实体过滤 <input v-model="filterEntity" placeholder="self / mother / project:..." @keydown.enter="reload" /></label>
      <label>条数上限
        <input type="number" min="10" max="500" step="10" v-model.number="limit" />
      </label>
      <label class="check">
        <input type="checkbox" v-model="includeSensitive" /> 包含 sensitive (health)
      </label>
      <button type="button" @click="reload">应用过滤</button>
    </div>

    <table class="records kg-table">
      <thead>
        <tr>
          <th>主语</th>
          <th>谓词</th>
          <th>宾语</th>
          <th>valid_from</th>
          <th>valid_to</th>
          <th>状态</th>
          <th>置信</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(t, i) in timeline" :key="t.id ?? `t-${i}`" :class="{ ended: !!t.valid_to }">
          <td><code>{{ t.subject }}</code></td>
          <td>
            <span class="pred">{{ t.predicate }}</span>
            <span class="muted small"> {{ predicateZh(t.predicate) }}</span>
          </td>
          <td><code>{{ t.object }}</code></td>
          <td class="mono">{{ t.valid_from ?? '—' }}</td>
          <td class="mono">{{ t.valid_to ?? '—' }}</td>
          <td>
            <span v-if="!t.valid_to" class="tag ok">current</span>
            <span v-else class="tag muted">ended</span>
          </td>
          <td>{{ t.confidence?.toFixed(2) ?? '—' }}</td>
        </tr>
        <tr v-if="!timeline.length && !loading">
          <td colspan="7" class="muted">无三元组（先发布或调整过滤）</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
