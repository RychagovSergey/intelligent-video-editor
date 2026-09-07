import type {
  AnalyzeRequest, AnalyzeStatus, FileMeta, Folder, Health, MediaList, ModelsInfo,
  AgentInfo, AgentStatus, ExportRequest, ExportStatus, PreviewStatus, ProjectInfo,
  RootInfo, ScanStatus,
  StorageStats, TextAnimation, TextPosition, Timeline, TimelineState, TransitionKind, WaveformOut,
} from './types'

/** FastAPI отдаёт `detail` строкой (HTTPException) либо списком ошибок валидации (422). */
function errorMessage(body: { detail?: unknown }, res: Response): string {
  const { detail } = body
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((e) => (e && typeof e === 'object' && 'msg' in e ? String((e as { msg: unknown }).msg) : String(e)))
      .join('; ')
  }
  return `${res.status} ${res.statusText}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(errorMessage(body, res))
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => request<Health>('/api/health'),
  stats: () => request<StorageStats>('/api/stats'),

  roots: () => request<{ roots: RootInfo[] }>('/api/storage/roots'),
  setRoots: (roots: string[]) =>
    request<{ roots: RootInfo[] }>('/api/storage/roots', {
      method: 'PUT',
      body: JSON.stringify({ roots }),
    }),

  startScan: () => request<ScanStatus>('/api/scan', { method: 'POST' }),
  scanStatus: () => request<ScanStatus>('/api/scan/status'),

  folders: (parentId?: number) =>
    request<Folder[]>(parentId == null ? '/api/folders' : `/api/folders?parent_id=${parentId}`),

  media: (params: {
    folderId?: number
    recursive?: boolean
    status?: string
    type?: string
    q?: string
  }) => {
    const sp = new URLSearchParams()
    if (params.folderId != null) sp.set('folder_id', String(params.folderId))
    if (params.recursive) sp.set('recursive', 'true')
    if (params.status) sp.set('status', params.status)
    if (params.type) sp.set('type', params.type)
    if (params.q) sp.set('q', params.q)
    return request<MediaList>(`/api/media?${sp.toString()}`)
  },

  meta: (fileId: number) => request<FileMeta>(`/api/media/${fileId}/meta`),

  thumbnailUrl: (fileId: number, width?: number) =>
    `/api/media/${fileId}/thumbnail${width ? `?width=${width}` : ''}`,
  mediaFileUrl: (fileId: number) => `/api/media/${fileId}/file`,
  playable: (fileId: number) =>
    request<{ kind: string; extension: string; native: boolean }>(`/api/media/${fileId}/playable`),
  playInFfplay: (fileId: number) =>
    request<{ playing: string }>(`/api/media/${fileId}/play`, { method: 'POST' }),

  analyze: (body: AnalyzeRequest) =>
    request<AnalyzeStatus>('/api/analyze', { method: 'POST', body: JSON.stringify(body) }),
  analyzeStatus: () => request<AnalyzeStatus>('/api/analyze/status'),
  cancelAnalysis: () => request<AnalyzeStatus>('/api/analyze/cancel', { method: 'POST' }),
  models: () => request<ModelsInfo>('/api/analyze/models'),
  projects: () => request<ProjectInfo[]>('/api/projects'),
  createProject: (name: string) =>
    request<ProjectInfo>('/api/projects', { method: 'POST', body: JSON.stringify({ name }) }),
  deleteProject: (id: number) =>
    request<{ deleted: number }>(`/api/projects/${id}`, { method: 'DELETE' }),

  timeline: (id: number) => request<TimelineState>(`/api/projects/${id}/timeline`),
  putTimeline: (id: number, timeline: Timeline) =>
    request<TimelineState>(`/api/projects/${id}/timeline`, { method: 'PUT', body: JSON.stringify(timeline) }),

  waveform: (sourceId: number, points = 800) =>
    request<WaveformOut>(`/api/media/${sourceId}/waveform?points=${points}`),

  addClip: (id: number, body: { source_id: number; start?: number; track_id?: string; duration?: number }) =>
    request<TimelineState>(`/api/projects/${id}/clips`, { method: 'POST', body: JSON.stringify(body) }),
  moveClip: (id: number, clipId: string, start: number, trackId?: string) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/move`, {
      method: 'PATCH', body: JSON.stringify({ start, track_id: trackId }),
    }),
  trimClip: (id: number, clipId: string, body: { in_point?: number; out_point?: number; keep_start?: boolean }) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/trim`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),
  splitClip: (id: number, clipId: string, at: number) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/split`, {
      method: 'POST', body: JSON.stringify({ at }),
    }),
  setClipSpeed: (id: number, clipId: string, speed: number) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/speed`, {
      method: 'PATCH', body: JSON.stringify({ speed }),
    }),
  muteClip: (id: number, clipId: string, muted: boolean) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/mute`, {
      method: 'PATCH', body: JSON.stringify({ muted }),
    }),
  setClipFade: (id: number, clipId: string, body: { fade_in?: number; fade_out?: number }) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/fade`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),
  setClipTransition: (id: number, clipId: string, kind: TransitionKind, duration: number) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/transition`, {
      method: 'PATCH', body: JSON.stringify({ kind, duration }),
    }),
  clearClipTransition: (id: number, clipId: string) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/transition`, { method: 'DELETE' }),
  addTextClip: (id: number, body: {
    text: string; duration: number; start?: number
    font_size?: number; position?: TextPosition; animation?: TextAnimation
  }) =>
    request<TimelineState>(`/api/projects/${id}/text_clips`, {
      method: 'POST', body: JSON.stringify(body),
    }),
  setTextProperties: (id: number, clipId: string, body: {
    text?: string; font_size?: number; position?: TextPosition; animation?: TextAnimation
  }) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}/text`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),
  deleteClip: (id: number, clipId: string) =>
    request<TimelineState>(`/api/projects/${id}/clips/${clipId}`, { method: 'DELETE' }),
  closeGaps: (id: number, trackId: string) =>
    request<TimelineState>(`/api/projects/${id}/tracks/${trackId}/close_gaps`, { method: 'POST' }),
  preview: (id: number, player: 'embedded' | 'ffplay' = 'embedded') =>
    request<PreviewStatus>(`/api/projects/${id}/preview?player=${player}`, { method: 'POST' }),
  previewStatus: () => request<PreviewStatus>('/api/projects/preview/status'),
  cancelPreview: () => request<PreviewStatus>('/api/projects/preview/cancel', { method: 'POST' }),

  export: (id: number, body: ExportRequest) =>
    request<ExportStatus>(`/api/projects/${id}/export`, { method: 'POST', body: JSON.stringify(body) }),
  exportStatus: () => request<ExportStatus>('/api/projects/export/status'),
  cancelExport: () => request<ExportStatus>('/api/projects/export/cancel', { method: 'POST' }),
  revealExport: () => request<{ revealed: string }>('/api/projects/export/reveal', { method: 'POST' }),

  undo: (id: number) => request<TimelineState>(`/api/projects/${id}/undo`, { method: 'POST' }),
  redo: (id: number) => request<TimelineState>(`/api/projects/${id}/redo`, { method: 'POST' }),

  agentInfo: () => request<AgentInfo>('/api/agent/info'),
  runAgent: (projectId: number, prompt: string) =>
    request<AgentStatus>('/api/agent/run', {
      method: 'POST', body: JSON.stringify({ project_id: projectId, prompt }),
    }),
  agentStatus: () => request<AgentStatus>('/api/agent/status'),
  cancelAgent: () => request<AgentStatus>('/api/agent/cancel', { method: 'POST' }),
  reindex: () => request<{ indexed: number; total: number; model: string }>('/api/agent/reindex', { method: 'POST' }),

  estimate: (folderId?: number) =>
    request<{ pending_total: number; pending_video: number; pending_image: number; pending_audio: number }>(
      `/api/analyze/estimate${folderId != null ? `?folder_id=${folderId}&recursive=true` : ''}`,
    ),
}
