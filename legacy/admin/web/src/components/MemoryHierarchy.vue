<script setup lang="ts">
import { ref, watch } from 'vue'
import type { HierarchyWingOut, MemPalaceHierarchyResponse } from '../api/client'
import { fetchHierarchy } from '../api/client'
import { useAdminUserId } from '../composables/useAdminUser'

const userId = useAdminUserId()
const maxRecords = ref(8000)
const maxDrawersPreview = ref(48)
const data = ref<MemPalaceHierarchyResponse | null>(null)
const err = ref('')
const loading = ref(false)

async function load() {
  err.value = ''
  loading.value = true
  data.value = null
  try {
    const p = new URLSearchParams({
      max_records: String(maxRecords.value),
      max_drawers_per_room: String(maxDrawersPreview.value),
    })
    data.value = await fetchHierarchy(userId.value, p)
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

watch([userId, maxRecords, maxDrawersPreview], load, { immediate: true })

function wingTitle(w: HierarchyWingOut): string {
  if (w.display_name) {
    return `${w.wing_id} — ${w.display_name}`
  }
  return w.wing_id
}
</script>

<template>
  <section class="panel hierarchy-page">
    <h2>MemPalace 层级总览</h2>
    <p class="muted">
      用户 <code>{{ userId }}</code> 的宫殿 · 自上而下四层：<strong>宫殿 → 翼 → 阁 → 抽屉</strong>。
    </p>

    <div class="row gap knobs">
      <label>max_records <input v-model.number="maxRecords" type="number" min="50" step="500" /></label>
      <label>每阁预览抽屉数<input v-model.number="maxDrawersPreview" type="number" min="4" /></label>
      <button type="button" :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="loading" class="muted">扫描并聚合层级…</p>
    <p v-if="err" class="error">{{ err }}</p>

    <template v-if="data && !loading">
      <article class="layers-summary">
        <h3>四层架构定义</h3>
        <ol class="tier-list">
          <li v-for="layer in data.layers" :key="layer.level">
            <strong>{{ layer.title }}</strong>
            <span class="level-tag mono">{{ layer.level }}</span>
            <p>{{ layer.description }}</p>
          </li>
        </ol>
      </article>

      <article class="palace-strip">
        <h3>当前宫殿</h3>
        <p class="mono path">{{ data.palace_path }}</p>
        <p class="muted">
          Steward 模式：<code>{{ data.steward_mode }}</code> · 本轮扫描聚合
          <strong>{{ data.total_records_scanned }}</strong>
          条抽屉记录
          <span v-if="data.capped_by_max_records" class="warn">
            （库中还有更多记录未被纳入本轮扫描，可提高 max_records）
          </span>
        </p>
      </article>

      <article>
        <h3>Room 命名参考（编排约定）</h3>
        <ul class="hint-list">
          <li v-for="(hint, idx) in data.room_naming_conventions" :key="idx" class="hint-li">
            {{ hint }}
          </li>
        </ul>
      </article>

      <article>
        <h3>已配置翼 + 实测结构</h3>
        <p class="muted">每个翼均可展开查看其下的 Room 与抽屉预览。</p>
        <div v-for="w in data.configured_wings" :key="w.wing_id" class="wing-block">
          <details open>
            <summary>
              <span class="summary-title">{{ wingTitle(w) }}</span>
              <span class="stats mono">{{ w.room_count }} 阁 · {{ w.drawer_count }} 抽屉</span>
            </summary>
            <p v-if="w.description" class="wing-desc muted">{{ w.description }}</p>
            <details v-for="room in w.rooms" :key="room.room_id" class="room-nested">
              <summary>
                <span class="mono">{{ room.room_id }}</span>
                <span class="stats muted">{{ room.drawer_count }} 抽屉</span>
                <span v-if="room.preview_truncated" class="warn tiny">预览截断</span>
              </summary>
              <ul class="drawer-list">
                <li v-for="d in room.drawers_preview" :key="d.key">
                  <span class="mono drawer-key">{{ d.key }}</span>
                  <span class="drawer-preview">{{ d.preview }}</span>
                </li>
              </ul>
            </details>
          </details>
        </div>
      </article>

      <article v-if="data.extra_wings.length">
        <h3>数据中多出的翼（未出现在 YAML）</h3>
        <p class="muted">例如历史数据把租户写在 <code>wing</code> 字段时可在此出现。</p>
        <div v-for="w in data.extra_wings" :key="w.wing_id" class="wing-block extra">
          <details>
            <summary>
              <span class="summary-title mono">{{ w.wing_id }}</span>
              <span class="stats mono">{{ w.room_count }} 阁 · {{ w.drawer_count }} 抽屉</span>
            </summary>
            <details v-for="room in w.rooms" :key="room.room_id" class="room-nested">
              <summary>
                <span class="mono">{{ room.room_id }}</span>
                <span class="stats muted">{{ room.drawer_count }}</span>
              </summary>
              <ul class="drawer-list">
                <li v-for="d in room.drawers_preview" :key="d.key">
                  <span class="mono drawer-key">{{ d.key }}</span>
                  <span class="drawer-preview">{{ d.preview }}</span>
                </li>
              </ul>
            </details>
          </details>
        </div>
      </article>
    </template>
  </section>
</template>
