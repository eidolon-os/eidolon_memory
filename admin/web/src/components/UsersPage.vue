<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  createUser,
  fetchUsersAll,
  initUserPalace,
  startUserAgent,
  stopUserAgent,
  toggleUserEnabled,
  type UserCreateRequest,
  type UserDetail,
  type UsersListResponse,
} from '../api/client'

const list = ref<UsersListResponse | null>(null)
const loading = ref(false)
const err = ref('')
const busy = ref<Record<string, string>>({})

// create form
const showCreate = ref(false)
const draft = ref<UserCreateRequest>({
  id: '',
  port: 8031,
  enabled: true,
  palace_path: '',
  init_palace: true,
  auto_start: true,
})
const createErr = ref('')
const createBusy = ref(false)

async function reload() {
  loading.value = true
  err.value = ''
  try {
    list.value = await fetchUsersAll()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
onMounted(reload)

const nextPort = computed(() => {
  if (!list.value?.users?.length) return 8030
  return Math.max(...list.value.users.map((u) => u.port)) + 1
})

function openCreate() {
  draft.value = {
    id: '',
    port: nextPort.value,
    enabled: true,
    palace_path: '',
    init_palace: true,
    auto_start: true,
  }
  createErr.value = ''
  showCreate.value = true
}

async function submitCreate() {
  if (!draft.value.id.trim()) {
    createErr.value = 'id 必填'
    return
  }
  createBusy.value = true
  createErr.value = ''
  try {
    await createUser({
      ...draft.value,
      id: draft.value.id.trim(),
      palace_path: draft.value.palace_path?.trim() || '',
    })
    showCreate.value = false
    await reload()
  } catch (e) {
    createErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    createBusy.value = false
  }
}

async function withBusy(uid: string, label: string, fn: () => Promise<unknown>) {
  busy.value = { ...busy.value, [uid]: label }
  err.value = ''
  try {
    await fn()
    await reload()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    const next = { ...busy.value }
    delete next[uid]
    busy.value = next
  }
}

const doInit = (u: UserDetail) => withBusy(u.user_id, '初始化', () => initUserPalace(u.user_id))
const doStart = (u: UserDetail) => withBusy(u.user_id, '启动', () => startUserAgent(u.user_id))
const doStop = (u: UserDetail) => withBusy(u.user_id, '停止', () => stopUserAgent(u.user_id))
const doToggle = (u: UserDetail) =>
  withBusy(u.user_id, u.enabled ? '禁用' : '启用', () =>
    toggleUserEnabled(u.user_id, !u.enabled),
  )
</script>

<template>
  <section class="panel users-page">
    <header class="panel-head">
      <div>
        <h2>用户管理</h2>
        <p class="muted small">
          users.yaml @ <code>{{ list?.users_yaml ?? '…' }}</code> ｜ admin 可直接 spawn/kill
          子进程 (eidolon-memory-agent) — 仅管理 admin 自己启动的;外部进程(supervisor /
          shell)只可见、不可停。
        </p>
      </div>
      <div class="row gap">
        <button type="button" class="primary" @click="openCreate">+ 新建用户</button>
        <button type="button" :disabled="loading" @click="reload">
          {{ loading ? '加载…' : '刷新' }}
        </button>
      </div>
    </header>

    <p v-if="err" class="error">{{ err }}</p>

    <div class="users-grid">
      <article
        v-for="u in list?.users"
        :key="u.user_id"
        :class="['user-card', { disabled: !u.enabled }]"
      >
        <header class="user-card-head">
          <div>
            <h3>{{ u.user_id }}</h3>
            <p class="muted small">
              port <code>{{ u.port }}</code>
              ·
              <a :href="u.mcp_http_url" target="_blank" class="mono small">{{ u.mcp_http_url }}</a>
            </p>
          </div>
          <div class="user-tags">
            <span v-if="!u.enabled" class="tag muted">disabled</span>
            <span v-else-if="u.agent_reachable" class="tag ok">running</span>
            <span v-else class="tag warn">stopped</span>
            <span v-if="u.palace_initialized" class="tag ok">palace ✓</span>
            <span v-else class="tag warn">palace ✗</span>
            <span v-if="u.managed_by_admin" class="tag" title="admin spawned this">admin-spawned</span>
            <span v-else-if="u.agent_reachable" class="tag" title="started by supervisor or shell">external</span>
          </div>
        </header>

        <dl class="user-card-meta">
          <div>
            <dt>palace</dt>
            <dd class="mono small">{{ u.palace_path }}</dd>
          </div>
          <div v-if="u.pid">
            <dt>pid</dt>
            <dd class="mono small">{{ u.pid }}</dd>
          </div>
          <div v-if="u.log_path">
            <dt>log</dt>
            <dd class="mono small">{{ u.log_path }}</dd>
          </div>
        </dl>

        <div class="user-actions row gap">
          <button
            v-if="!u.palace_initialized"
            type="button"
            :disabled="!!busy[u.user_id]"
            @click="doInit(u)"
          >
            {{ busy[u.user_id] === '初始化' ? '初始化中…' : '初始化 palace' }}
          </button>
          <button
            v-if="!u.agent_reachable && u.enabled"
            type="button"
            class="primary"
            :disabled="!!busy[u.user_id] || !u.palace_initialized"
            @click="doStart(u)"
          >
            {{ busy[u.user_id] === '启动' ? '启动中…' : '启动 agent' }}
          </button>
          <button
            v-if="u.managed_by_admin"
            type="button"
            class="danger"
            :disabled="!!busy[u.user_id]"
            @click="doStop(u)"
          >
            {{ busy[u.user_id] === '停止' ? '停止中…' : '停止' }}
          </button>
          <button
            type="button"
            :disabled="!!busy[u.user_id]"
            @click="doToggle(u)"
          >
            {{ u.enabled ? '禁用 (yaml)' : '启用 (yaml)' }}
          </button>
        </div>
      </article>

      <article v-if="!list?.users.length && !loading" class="user-card empty">
        <p class="muted">users.yaml 为空。点 "新建用户" 添加一个。</p>
      </article>
    </div>

    <!-- create modal -->
    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate = false">
      <form class="modal panel" @submit.prevent="submitCreate">
        <header class="panel-head">
          <h3>新建用户</h3>
          <button type="button" class="btn-x" @click="showCreate = false">✕</button>
        </header>

        <div class="grid">
          <label>id <input v-model="draft.id" required placeholder="alice" /></label>
          <label>port <input type="number" v-model.number="draft.port" min="1024" max="65535" /></label>
        </div>

        <label class="block">
          palace_path (可选, 留空走默认 ~/eidolon/memory/mempalaces/&lt;id&gt;/)
          <input v-model="draft.palace_path" placeholder="" />
        </label>

        <div class="row gap">
          <label class="check">
            <input type="checkbox" v-model="draft.init_palace" /> 自动 init palace
          </label>
          <label class="check">
            <input type="checkbox" v-model="draft.auto_start" /> 自动启动 agent
          </label>
          <label class="check">
            <input type="checkbox" v-model="draft.enabled" /> enabled 写入 yaml
          </label>
        </div>

        <p v-if="createErr" class="error small">{{ createErr }}</p>

        <div class="row gap modal-actions">
          <button type="button" @click="showCreate = false">取消</button>
          <button type="submit" class="primary" :disabled="createBusy">
            {{ createBusy ? '创建中…' : '创建' }}
          </button>
        </div>
      </form>
    </div>
  </section>
</template>
