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

export interface HealthResponse {
  ok: boolean
  palace_path: string
  steward_mode: string
}

export interface MemoryRecord {
  user_id: string
  key: string
  value: unknown
  metadata: Record<string, unknown>
  created_at?: string | null
  updated_at?: string | null
}

export async function fetchHealth(): Promise<HealthResponse> {
  return fetchApi<HealthResponse>('/health')
}

export async function fetchMemoryList(params: URLSearchParams): Promise<{
  records: MemoryRecord[]
  total_hint?: number | null
}> {
  return fetchApi(`/memories?${params.toString()}`)
}

export async function fetchMemorySearch(params: URLSearchParams): Promise<{ records: MemoryRecord[] }> {
  return fetchApi(`/memories/search?${params.toString()}`)
}

export async function postMemory(body: {
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

export async function deleteMemoryKey(key: string): Promise<void> {
  await fetchApi(`/memories/${encodeURIComponent(key)}`, { method: 'DELETE' })
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

export async function fetchHierarchy(params: URLSearchParams): Promise<MemPalaceHierarchyResponse> {
  const q = params.toString()
  return fetchApi<MemPalaceHierarchyResponse>(`/hierarchy${q ? `?${q}` : ''}`)
}
