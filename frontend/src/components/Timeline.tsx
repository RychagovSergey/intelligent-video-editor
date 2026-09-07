import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { formatDuration } from '../format'
import { ClipPropertiesModal } from './ClipPropertiesModal'
import type { Clip, TimelineState, Track, WaveformOut } from '../api/types'

const MIN_PX_PER_SEC = 4
const MAX_PX_PER_SEC = 200
const TRACK_HEIGHT = 56
const HANDLE_WIDTH = 7

// Только порядок отображения — текст рисуется поверх видео при рендере (compile.py),
// так что в списке дорожек ему логичнее быть над видео, а не под аудио. Сама модель
// (state.timeline.tracks) порядок не меняет — от него зависят track_id по умолчанию.
const TRACK_DISPLAY_ORDER: Record<string, number> = { text: 0, video: 1, audio: 2 }

type DragMode = 'move' | 'trim-left' | 'trim-right'

interface Drag {
  mode: DragMode
  clip: Clip
  track: Track
  startX: number
  originalStart: number
  originalIn: number
  originalOut: number
  offsetSeconds: number
}

interface Props {
  state: TimelineState | null
  selectedClipId: string | null
  onSelectClip: (clipId: string | null) => void
  onChange: (next: TimelineState) => void
  onError: (message: string) => void
  playhead: number
  onPlayhead: (seconds: number) => void
}

/** Таймлайн на обычном DOM: клипы — абсолютно спозиционированные блоки (ТЗ п. 3.3). */
export function Timeline({
  state, selectedClipId, onSelectClip, onChange, onError, playhead, onPlayhead,
}: Props) {
  const [pxPerSec, setPxPerSec] = useState(40)
  const [drag, setDrag] = useState<Drag | null>(null)
  const [draggingPlayhead, setDraggingPlayhead] = useState(false)
  const [ghost, setGhost] = useState<{ trackId: string; start: number; duration: number } | null>(null)
  const [waveforms, setWaveforms] = useState<Map<number, WaveformOut>>(new Map())
  const [editingClip, setEditingClip] = useState<Clip | null>(null)
  const fetchingWaveforms = useRef<Set<number>>(new Set())
  const scrollRef = useRef<HTMLDivElement>(null)

  // Waveform запрашивается один раз на исходный файл (не на клип) и переиспользуется
  // всеми клипами этого файла — обрезка/повтор клипа не требует повторного запроса.
  useEffect(() => {
    if (!state) return
    const audioSourceIds = new Set<number>()
    for (const track of state.timeline.tracks) {
      for (const clip of track.clips) {
        if (clip.kind === 'audio') audioSourceIds.add(clip.source_id)
      }
    }
    for (const sourceId of audioSourceIds) {
      if (waveforms.has(sourceId) || fetchingWaveforms.current.has(sourceId)) continue
      fetchingWaveforms.current.add(sourceId)
      api.waveform(sourceId)
        .then((wf) => setWaveforms((prev) => new Map(prev).set(sourceId, wf)))
        .catch(() => {})   // клип просто останется без waveform
        .finally(() => fetchingWaveforms.current.delete(sourceId))
    }
  }, [state, waveforms])

  const duration = state?.duration ?? 0
  // Всегда оставляем запас справа, чтобы было куда перетаскивать клипы.
  const visibleSeconds = Math.max(duration + 10, 30)
  const contentWidth = visibleSeconds * pxPerSec

  const call = useCallback(
    async (action: () => Promise<TimelineState>) => {
      try {
        onChange(await action())
      } catch (e) {
        onError((e as Error).message)
      }
    },
    [onChange, onError],
  )

  const timeAt = useCallback(
    (clientX: number): number => {
      const box = scrollRef.current?.getBoundingClientRect()
      const scroll = scrollRef.current?.scrollLeft ?? 0
      if (!box) return 0
      return Math.max(0, (clientX - box.left + scroll) / pxPerSec)
    },
    [pxPerSec],
  )

  // Перетаскивание и подрезка ведутся мышью на всём окне: курсор может уйти за клип.
  useEffect(() => {
    if (!drag || !state) return

    const onMove = (event: MouseEvent) => {
      const deltaSeconds = (event.clientX - drag.startX) / pxPerSec
      if (drag.mode === 'move') {
        const start = Math.max(0, drag.originalStart + deltaSeconds)
        setGhost({ trackId: drag.track.id, start, duration: clipDuration(drag.clip) })
      } else if (drag.mode === 'trim-left') {
        const inPoint = clamp(drag.originalIn + deltaSeconds * drag.clip.speed, 0, drag.originalOut - 0.1)
        setGhost({
          trackId: drag.track.id,
          start: drag.originalStart,
          duration: (drag.originalOut - inPoint) / drag.clip.speed,
        })
      } else {
        const outPoint = Math.max(drag.originalIn + 0.1, drag.originalOut + deltaSeconds * drag.clip.speed)
        setGhost({
          trackId: drag.track.id,
          start: drag.originalStart,
          duration: (outPoint - drag.originalIn) / drag.clip.speed,
        })
      }
    }

    const onUp = (event: MouseEvent) => {
      const deltaSeconds = (event.clientX - drag.startX) / pxPerSec
      setDrag(null)
      setGhost(null)
      if (Math.abs(event.clientX - drag.startX) < 3) return   // клик, а не перетаскивание

      if (drag.mode === 'move') {
        const start = Math.max(0, round(drag.originalStart + deltaSeconds))
        void call(() => api.moveClip(state.project_id, drag.clip.id, start))
      } else if (drag.mode === 'trim-left') {
        const inPoint = round(clamp(drag.originalIn + deltaSeconds * drag.clip.speed, 0, drag.originalOut - 0.1))
        void call(() => api.trimClip(state.project_id, drag.clip.id, { in_point: inPoint, keep_start: true }))
      } else {
        const outPoint = round(Math.max(drag.originalIn + 0.1, drag.originalOut + deltaSeconds * drag.clip.speed))
        void call(() => api.trimClip(state.project_id, drag.clip.id, { out_point: outPoint }))
      }
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [drag, pxPerSec, state, call])

  // Курсор таскается мышью по всему окну: он узкий, палец с него легко соскакивает.
  useEffect(() => {
    if (!draggingPlayhead) return
    const onMove = (event: MouseEvent) => onPlayhead(round(timeAt(event.clientX)))
    const onUp = () => setDraggingPlayhead(false)
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [draggingPlayhead, timeAt, onPlayhead])

  const ticks = useMemo(() => buildTicks(visibleSeconds, pxPerSec), [visibleSeconds, pxPerSec])
  const displayTracks = useMemo(
    () => (state ? [...state.timeline.tracks].sort(
      (a, b) => (TRACK_DISPLAY_ORDER[a.kind] ?? 99) - (TRACK_DISPLAY_ORDER[b.kind] ?? 99)
    ) : []),
    [state],
  )

  if (!state) {
    return (
      <div className="placeholder" style={{ flex: 1 }}>
        <div style={{ fontSize: 15 }}>Проект не открыт</div>
        <div>Создайте проект в верхней панели, затем перетащите файлы сюда</div>
      </div>
    )
  }

  const startDrag = (event: React.MouseEvent, mode: DragMode, clip: Clip, track: Track) => {
    event.stopPropagation()
    onSelectClip(clip.id)
    setDrag({
      mode, clip, track,
      startX: event.clientX,
      originalStart: clip.start,
      originalIn: clip.in_point,
      originalOut: clip.out_point,
      offsetSeconds: timeAt(event.clientX) - clip.start,
    })
  }

  const onDrop = (event: React.DragEvent, track: Track) => {
    event.preventDefault()
    if (track.kind === 'text') return   // текстовые слои добавляются агентом/своим инструментом, не файлом
    const sourceId = Number(event.dataTransfer.getData('application/x-media-id'))
    if (!sourceId) return
    const start = round(timeAt(event.clientX))
    void call(() => api.addClip(state.project_id, { source_id: sourceId, start, track_id: track.id }))
  }

  return (
    <div className="timeline">
      <div className="timeline-toolbar">
        <button onClick={() => setPxPerSec((v) => clamp(v / 1.5, MIN_PX_PER_SEC, MAX_PX_PER_SEC))} title="Отдалить">−</button>
        <span className="zoom-label">{Math.round(pxPerSec)} px/с</span>
        <button onClick={() => setPxPerSec((v) => clamp(v * 1.5, MIN_PX_PER_SEC, MAX_PX_PER_SEC))} title="Приблизить">+</button>
        <span className="divider" />
        <span>Курсор: {formatDuration(playhead)}</span>
        <span>Длительность: {formatDuration(duration)}</span>
      </div>

      <div
        className="timeline-scroll"
        ref={scrollRef}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest('.clip')) return
          onSelectClip(null)
          onPlayhead(round(timeAt(e.clientX)))
        }}
      >
        <div className="timeline-content" style={{ width: contentWidth }}>
          <div className="ruler">
            {ticks.map((tick) => (
              <span key={tick.seconds} className="tick" style={{ left: tick.seconds * pxPerSec }}>
                {tick.label}
              </span>
            ))}
          </div>

          {displayTracks.map((track) => (
            <div
              key={track.id}
              className={`track track-${track.kind}`}
              style={{ height: TRACK_HEIGHT }}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => onDrop(e, track)}
            >
              <span className="track-label">{track.name}</span>

              {track.clips.map((clip) => {
                const isDragging = ghost && drag?.clip.id === clip.id
                const left = (isDragging ? ghost!.start : clip.start) * pxPerSec
                const width = (isDragging ? ghost!.duration : clipDuration(clip)) * pxPerSec
                return (
                  <div
                    key={clip.id}
                    className={`clip clip-${clip.kind}${selectedClipId === clip.id ? ' selected' : ''}`}
                    style={{ left, width: Math.max(width, 6) }}
                    onMouseDown={(e) => startDrag(e, 'move', clip, track)}
                    onClick={(e) => { e.stopPropagation(); onSelectClip(clip.id) }}
                    onDoubleClick={(e) => { e.stopPropagation(); setEditingClip(clip) }}
                    title={`${clip.name} · ${formatDuration(clipDuration(clip))}${clip.speed !== 1 ? ` · ${clip.speed}x` : ''} · двойной клик — параметры`}
                  >
                    <span
                      className="clip-handle left"
                      style={{ width: HANDLE_WIDTH }}
                      onMouseDown={(e) => startDrag(e, 'trim-left', clip, track)}
                    />
                    {clip.kind === 'audio' && <ClipWaveform clip={clip} waveform={waveforms.get(clip.source_id)} />}
                    <span className="clip-name">
                      {clip.name}
                      {clip.speed !== 1 && <b> {clip.speed}×</b>}
                    </span>
                    <span
                      className="clip-handle right"
                      style={{ width: HANDLE_WIDTH }}
                      onMouseDown={(e) => startDrag(e, 'trim-right', clip, track)}
                    />
                  </div>
                )
              })}
            </div>
          ))}

          <div
            className={`playhead${draggingPlayhead ? ' dragging' : ''}`}
            style={{ left: playhead * pxPerSec }}
            onMouseDown={(e) => { e.stopPropagation(); setDraggingPlayhead(true) }}
            title="Перетащите, чтобы переместить текущий кадр"
          >
            <span className="playhead-grip" />
          </div>
        </div>
      </div>

      {editingClip && (
        <ClipPropertiesModal
          projectId={state.project_id}
          clip={editingClip}
          onClose={() => setEditingClip(null)}
          onChange={(next) => { onChange(next); setEditingClip(null) }}
        />
      )}
    </div>
  )
}

function clipDuration(clip: Clip): number {
  return (clip.out_point - clip.in_point) / clip.speed
}

/** Срез общей waveform исходника на [in_point, out_point] клипа, растянутый на всю его ширину. */
function ClipWaveform({ clip, waveform }: { clip: Clip; waveform?: WaveformOut }) {
  if (!waveform || !waveform.duration || waveform.points.length === 0) return null
  const { duration, points } = waveform
  const startIdx = Math.max(0, Math.floor((clip.in_point / duration) * points.length))
  const endIdx = Math.min(points.length, Math.ceil((clip.out_point / duration) * points.length))
  const slice = points.slice(startIdx, Math.max(startIdx + 1, endIdx))
  if (slice.length === 0) return null

  return (
    <svg className="clip-waveform" viewBox={`0 0 ${slice.length} 100`} preserveAspectRatio="none">
      {slice.map((amp, i) => {
        const height = Math.max(2, amp * 90)
        return <rect key={i} x={i} y={(100 - height) / 2} width={1} height={height} />
      })}
    </svg>
  )
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000
}

function buildTicks(seconds: number, pxPerSec: number): { seconds: number; label: string }[] {
  // Шаг подбирается так, чтобы подписи не слипались при любом масштабе.
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
  const step = steps.find((s) => s * pxPerSec >= 70) ?? steps[steps.length - 1]
  const ticks = []
  for (let t = 0; t <= seconds; t += step) {
    ticks.push({ seconds: t, label: formatDuration(t) })
  }
  return ticks
}
