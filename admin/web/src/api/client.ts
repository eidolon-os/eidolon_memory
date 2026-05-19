/** Calls FastAPI admin under `/api`; Vite proxies to backend in dev. */

function authHeaders(): HeadersInit {
  const headers: HeadersInit = {}
  const t = import.meta.env.VITE_ADMIN_TOKEN as string | undefined
  if (t?.trim()) {
    headers['Authorization'] = `Bearer ${t.trim()}`
  }
  return headers
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (body?.detail !== undefined) {
      return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    }
  } catch {
    /* ignore */
  }
  return await res.text()
}

export async function fetchApi<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers ?? {})
  for (const [k, v] of Object.entries(authHeaders())) {
    if (!headers.has(k)) headers.set(k, v as string)
  }
  const res = await fetch(`/api${path}`, { ...init, headers })
  if (!res.ok) {
    throw new Error(`${res.status}: ${await parseError(res)}`)
  }
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

export interface UserStatusOut {
  user_id: string
  port: number
  enabled: boolean
  palace_path: string
  mcp_http_url: string
  agent_reachable: boolean
  runner_status?: Record<string, unknown> | null
  runner_status_error?: string | null
}

export interface HealthResponse {
  ok: boolean
  steward_mode: string
  default_user_id: string
  users: UserStatusOut[]
}

export interface MemoryRecord {
  user_id: string
  key: string
  value: unknown
  metadata: Record<string, unknown>
  created_at?: string | null
  updated_at?: string | null
}

export function recordSimilarity(r: MemoryRecord): number | null {
  const sim = r.metadata?.similarity
  if (typeof sim === 'number') return sim
  if (typeof sim === 'string') {
    const n = Number(sim)
    return Number.isFinite(n) ? n : null
  }
  return null
}

export async function fetchHealth(): Promise<HealthResponse> {
  return fetchApi<HealthResponse>('/health')
}

export async function fetchMemoryList(
  userId: string,
  params: URLSearchParams,
): Promise<{
  records: MemoryRecord[]
  total_hint?: number | null
}> {
  const p = new URLSearchParams(params)
  p.set('user_id', userId)
  return fetchApi(`/memories?${p.toString()}`)
}

export async function fetchMemorySearch(
  userId: string,
  params: URLSearchParams,
): Promise<{ records: MemoryRecord[] }> {
  const p = new URLSearchParams(params)
  p.set('user_id', userId)
  return fetchApi(`/memories/search?${p.toString()}`)
}

export async function postMemory(body: {
  user_id: string
  wing: string
  room: string
  text: string
  metadata?: Record<string, unknown> | null
}): Promise<{ status: string; detail: string }> {
  return fetchApi(`/memories`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export interface MemPalaceLayerInfo {
  level: string
  title: string
  description: string
}

export interface HierarchyDrawerPreview {
  key: string
  preview: string
}

export interface HierarchyRoomOut {
  room_id: string
  drawer_count: number
  drawers_preview: HierarchyDrawerPreview[]
  preview_truncated: boolean
}

export interface HierarchyWingOut {
  wing_id: string
  is_configured: boolean
  display_name: string
  description: string
  sort_order: number
  room_count: number
  drawer_count: number
  rooms: HierarchyRoomOut[]
}

export interface MemPalaceHierarchyResponse {
  palace_path: string
  layers: MemPalaceLayerInfo[]
  room_naming_conventions: string[]
  steward_mode: string
  total_records_scanned: number
  capped_by_max_records: boolean
  configured_wings: HierarchyWingOut[]
  extra_wings: HierarchyWingOut[]
}

export async function fetchHierarchy(
  userId: string,
  params: URLSearchParams,
): Promise<MemPalaceHierarchyResponse> {
  const p = new URLSearchParams(params)
  p.set('user_id', userId)
  const q = p.toString()
  return fetchApi<MemPalaceHierarchyResponse>(`/hierarchy${q ? `?${q}` : ''}`)
}

export interface GraphNode {
  id: string
  label: string
  kind: string
  entity_type?: string | null
  wings?: string[] | null
  halls?: string[] | null
  count?: number | null
  is_tunnel?: boolean | null
}

export interface GraphEdge {
  id: string
  source: string
  target: string
  label: string
  valid_from?: string | null
  valid_to?: string | null
  current?: boolean | null
  shared_wings?: string[] | null
}

export interface KnowledgeGraphSnapshot {
  available: boolean
  palace_path: string
  kg_path: string
  stats: Record<string, unknown> | null
  nodes: GraphNode[]
  edges: GraphEdge[]
  capped: boolean
  triple_count?: number | null
  reason?: string | null
}

export interface PalaceGraphSnapshot {
  available: boolean
  palace_path: string
  stats: Record<string, unknown> | null
  nodes: GraphNode[]
  edges: GraphEdge[]
  capped: boolean
  total_rooms?: number | null
  reason?: string | null
}

export async function fetchKnowledgeGraph(
  userId: string,
  params?: URLSearchParams,
): Promise<KnowledgeGraphSnapshot> {
  const p = new URLSearchParams(params)
  p.set('user_id', userId)
  return fetchApi<KnowledgeGraphSnapshot>(`/graph/knowledge?${p.toString()}`)
}

export async function fetchPalaceGraph(
  userId: string,
  params?: URLSearchParams,
): Promise<PalaceGraphSnapshot> {
  const p = new URLSearchParams(params)
  p.set('user_id', userId)
  return fetchApi<PalaceGraphSnapshot>(`/graph/palace?${p.toString()}`)
}

// ─── KG (T1+T2+T3) ─────────────────────────────────────────────────────

export interface KgTripleOut {
  id?: string | null
  subject: string
  predicate: string
  object: string
  valid_from?: string | null
  valid_to?: string | null
  confidence?: number | null
  source_drawer_id?: string | null
  adapter_name?: string | null
}

export interface KgStats {
  entities: number
  triples_total: number
  triples_active: number
  triples_invalidated: number
}

export interface KgPredicates {
  predicates: string[]
  sensitive: string[]
  count: number
}

export interface KgEntityResponse {
  entity: string
  as_of?: string | null
  direction: string
  triples: KgTripleOut[]
}

export interface KgTimelineResponse {
  entity_name?: string | null
  since?: string | null
  until?: string | null
  events: KgTripleOut[]
}

export interface KgWriteResult {
  status: string
  request_id: string
  triple_id?: string | null
}

export async function fetchKgStats(userId: string): Promise<KgStats> {
  const p = new URLSearchParams({ user_id: userId })
  return fetchApi<KgStats>(`/kg/stats?${p.toString()}`)
}

export async function fetchKgPredicates(userId: string): Promise<KgPredicates> {
  const p = new URLSearchParams({ user_id: userId })
  return fetchApi<KgPredicates>(`/kg/predicates?${p.toString()}`)
}

export async function fetchKgEntity(
  userId: string,
  name: string,
  opts?: { direction?: string; includeSensitive?: boolean; asOf?: string },
): Promise<KgEntityResponse> {
  const p = new URLSearchParams({ user_id: userId })
  if (opts?.direction) p.set('direction', opts.direction)
  if (opts?.includeSensitive) p.set('include_sensitive', 'true')
  if (opts?.asOf) p.set('as_of', opts.asOf)
  return fetchApi<KgEntityResponse>(`/kg/entity/${encodeURIComponent(name)}?${p.toString()}`)
}

export async function fetchKgTimeline(
  userId: string,
  opts?: {
    entityName?: string
    since?: string
    until?: string
    limit?: number
    includeSensitive?: boolean
  },
): Promise<KgTimelineResponse> {
  const p = new URLSearchParams({ user_id: userId })
  if (opts?.entityName) p.set('entity_name', opts.entityName)
  if (opts?.since) p.set('since', opts.since)
  if (opts?.until) p.set('until', opts.until)
  if (opts?.limit) p.set('limit', String(opts.limit))
  if (opts?.includeSensitive) p.set('include_sensitive', 'true')
  return fetchApi<KgTimelineResponse>(`/kg/timeline?${p.toString()}`)
}

export async function postKgTriple(
  userId: string,
  body: Omit<KgTripleAddRequest, 'user_id'>,
): Promise<KgWriteResult> {
  const p = new URLSearchParams({ user_id: userId })
  return fetchApi<KgWriteResult>(`/kg/triples?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, user_id: userId }),
  })
}

export async function postKgInvalidate(
  userId: string,
  body: Omit<KgInvalidateRequest, 'user_id'>,
): Promise<KgWriteResult> {
  const p = new URLSearchParams({ user_id: userId })
  return fetchApi<KgWriteResult>(`/kg/invalidations?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, user_id: userId }),
  })
}

export interface KgTripleAddRequest {
  user_id: string
  subject: string
  predicate: string
  object: string
  valid_from?: string | null
  valid_to?: string | null
  confidence?: number
  wait_visible_seconds?: number
}

export interface KgInvalidateRequest {
  user_id: string
  subject: string
  predicate: string
  object: string
  ended?: string | null
  wait_visible_seconds?: number
}

// ─── Recall (fused) ────────────────────────────────────────────────────

export interface RecallResponse {
  context: string
  kg_triples: KgTripleOut[]
  records: MemoryRecord[]
}

export async function postRecall(
  userId: string,
  body: {
    query: string
    top_k?: number
    voice?: boolean
    include_kg?: boolean | null
    include_sensitive_kg?: boolean
  },
): Promise<RecallResponse> {
  const p = new URLSearchParams({ user_id: userId })
  return fetchApi<RecallResponse>(`/recall?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
