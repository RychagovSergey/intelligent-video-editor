export type MediaType = 'video' | 'image' | 'audio'
export type AnalysisStatus = 'pending' | 'analyzed' | 'stale' | 'failed' | 'corrupted'

export interface Folder {
  id: number
  name: string
  path: string
  parent_id: number | null
  is_root: boolean
  missing: boolean
  file_count: number
  has_children: boolean
}

export interface MediaFile {
  id: number
  folder_id: number
  filename: string
  path: string
  type: MediaType
  size: number
  modified: number
  has_meta: boolean
  analysis_status: AnalysisStatus
  missing: boolean
  duration: number | null
  width: number | null
  height: number | null
  fps: number | null
  video_codec: string | null
  audio_codec: string | null
  bitrate: number | null
  has_audio: boolean
  has_cover_art: boolean
  probe_error: string | null
}

export interface MediaList {
  items: MediaFile[]
  total: number
  offset: number
  limit: number
}

export interface FileMeta {
  file_id: number
  model: string | null
  updated_at: string | null
  meta: Record<string, unknown> | null
}

export interface WaveformOut {
  duration: number
  points: number[]
}

export interface RootInfo {
  path: string
  exists: boolean
  is_dir: boolean
  writable: boolean
}

export interface ScanStats {
  roots: string[]
  folders: number
  files_total: number
  files_added: number
  files_updated: number
  files_missing: number
  meta_loaded: number
  unreadable: string[]
  errors: string[]
  probed?: number
  ok?: number
  corrupted?: number
}

export interface ScanStatus {
  state: 'idle' | 'running' | 'done' | 'error'
  phase: 'scanning' | 'probing' | null
  started_at: string | null
  finished_at: string | null
  current_root: string | null
  current_file: string | null
  done: number
  total: number
  stats: ScanStats | null
  error: string | null
}

export interface Health {
  status: string
  ffmpeg: string | null
  ffprobe: string | null
  ffplay: string | null
  vl_model: string
  vl_model_fast: string
  agent_provider: string
  agent_configured: boolean
}

export interface StorageStats {
  total: number
  by_type: Record<string, number>
  by_status: Record<string, number>
}

export interface AnalyzeStatus {
  state: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  model: string | null
  current_file: string | null
  current_step: string | null
  done: number
  total: number
  analyzed: number
  failed: number
  skipped: number
  errors: string[]
  error: string | null
}

export interface ModelsInfo {
  available: boolean
  error?: string
  models: string[]
  quality: string | null
  fast: string | null
  quality_installed?: boolean
  fast_installed?: boolean
}

export interface AnalyzeRequest {
  file_ids?: number[]
  folder_id?: number
  recursive?: boolean
  force?: boolean
  fast?: boolean
  max_frames?: number
}

// --- проект монтажа (этап 4) ---

export type ClipKind = 'video' | 'image' | 'audio' | 'text'
export type TrackKind = 'video' | 'audio' | 'text'
export type TransitionKind = 'crossfade' | 'dip_to_black'
export type TextPosition = 'top' | 'center' | 'bottom'
export type TextAnimation = 'none' | 'fade'

export interface Transition {
  kind: TransitionKind
  duration: number
}

export interface Clip {
  id: string
  source_id: number | null
  name: string
  kind: ClipKind
  start: number
  in_point: number
  out_point: number
  speed: number
  muted: boolean
  fade_in: number
  fade_out: number
  transition_in: Transition | null
  // только у kind: 'text'
  text?: string | null
  font_size?: number | null
  position?: TextPosition | null
  animation?: TextAnimation | null
}

export interface Track {
  id: string
  kind: TrackKind
  name: string
  muted: boolean
  clips: Clip[]
}

export interface Timeline {
  version: number
  fps: number
  width: number
  height: number
  tracks: Track[]
}

export interface TimelineState {
  project_id: number
  timeline: Timeline
  duration: number
  can_undo: boolean
  can_redo: boolean
  preview_ready: boolean
}

export interface ProjectInfo {
  id: number
  name: string
  created_at: string
  updated_at: string
  duration: number
}

export interface PreviewStatus {
  state: 'idle' | 'rendering' | 'ready' | 'playing' | 'done' | 'error' | 'cancelled'
  project_id: number | null
  done_seconds: number
  total_seconds: number
  output: string | null
  cached: boolean
  error: string | null
}

export interface ExportStatus {
  state: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  project_id: number | null
  output: string | null
  percent: number
  done_seconds: number
  total_seconds: number
  eta_seconds: number | null
  size_bytes: number | null
  error: string | null
}

export interface ExportRequest {
  directory: string
  filename?: string
  container: 'mp4' | 'mov' | 'webm'
  quality: 'high' | 'medium' | 'low'
  bitrate?: string | null
}

export interface AgentLogEntry {
  kind: 'tool' | 'error' | 'answer'
  text: string
  detail?: unknown
}

export interface AgentStatus {
  state: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  project_id: number | null
  prompt: string | null
  model: string | null
  step: number
  max_steps: number
  log: AgentLogEntry[]
  answer: string | null
  error: string | null
}

export interface AgentInfo {
  configured: boolean
  provider: string | null
  model: string | null
  error: string | null
  instructions_file: string
  instructions_found: boolean
}
