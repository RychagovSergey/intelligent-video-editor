import { useEffect, useRef, useState } from 'react'
import { formatDuration } from '../format'
import type { PreviewStatus } from '../api/types'

interface Props {
  projectId: number | null
  preview: PreviewStatus | null
  /** Таймлайн правили после сборки прокси — то, что играет, уже не соответствует проекту. */
  stale?: boolean
  playhead: number
  onPlayhead: (seconds: number) => void
  onRequestRender: () => void
}

/** Встроенный плеер прокси-файла: play/pause, перемотка, синхронизация с курсором таймлайна. */
export function PreviewPlayer({
  projectId, preview, stale = false, playhead, onPlayhead, onRequestRender,
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false)
  const [ready, setReady] = useState(false)
  // Пока курсор двигает сам плеер, не дёргаем его обратно на позицию таймлайна.
  const seekingFromPlayer = useRef(false)

  const hasProxy = projectId != null &&
    (preview?.state === 'ready' || preview?.state === 'playing' || preview?.state === 'done') &&
    preview?.project_id === projectId
  const src = hasProxy ? `/api/projects/${projectId}/preview/file` : null

  useEffect(() => {
    setReady(false)
    setPlaying(false)
  }, [src])

  // Курсор таймлайна переставили мышью — перематываем плеер.
  useEffect(() => {
    const video = videoRef.current
    if (!video || !ready || seekingFromPlayer.current) return
    if (Math.abs(video.currentTime - playhead) > 0.3) video.currentTime = playhead
  }, [playhead, ready])

  const toggle = () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) void video.play()
    else video.pause()
  }

  if (!src) {
    return (
      <div className="placeholder" style={{ flex: 1 }}>
        <div style={{ fontSize: 15 }}>Предпросмотр</div>
        <div>
          {projectId == null
            ? 'Откройте проект и добавьте клипы'
            : preview?.state === 'rendering'
              ? `Собираю прокси… ${Math.round(preview.done_seconds)}/${Math.round(preview.total_seconds)} с`
              : 'Нажмите «Собрать превью», чтобы отрендерить прокси'}
        </div>
        {projectId != null && preview?.state !== 'rendering' && (
          <button className="primary" style={{ marginTop: 6 }} onClick={onRequestRender}>
            Собрать превью
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="player">
      {stale && (
        <div className="stale-banner">
          Таймлайн изменился после сборки — показанное видео уже не соответствует проекту.
          <button onClick={onRequestRender}>Пересобрать</button>
        </div>
      )}
      <video
        ref={videoRef}
        className="player-video"
        src={src}
        onLoadedMetadata={() => {
          setReady(true)
          const video = videoRef.current
          if (video && playhead > 0 && playhead < video.duration) video.currentTime = playhead
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={() => {
          const video = videoRef.current
          if (!video) return
          seekingFromPlayer.current = true
          onPlayhead(Math.round(video.currentTime * 1000) / 1000)
          // Отпускаем блокировку в следующем тике, иначе эффект выше вернёт плеер назад.
          window.setTimeout(() => { seekingFromPlayer.current = false }, 0)
        }}
        onClick={toggle}
      />
      <div className="player-controls">
        <button onClick={toggle} className="primary" title={playing ? 'Пауза (пробел)' : 'Играть (пробел)'}>
          {playing ? '❚❚' : '▶'}
        </button>
        <button
          onClick={() => { const v = videoRef.current; if (v) v.currentTime = 0 }}
          title="В начало"
        >
          ⏮
        </button>
        <input
          className="player-seek"
          type="range"
          min={0}
          max={videoRef.current?.duration || preview?.total_seconds || 0}
          step={0.05}
          value={playhead}
          onChange={(e) => {
            const video = videoRef.current
            if (video) video.currentTime = Number(e.target.value)
            onPlayhead(Number(e.target.value))
          }}
        />
        <span className="player-time">
          {formatDuration(playhead)} / {formatDuration(videoRef.current?.duration || preview?.total_seconds || 0)}
        </span>
        <button
          onClick={onRequestRender}
          disabled={!stale}
          title={stale ? 'Собрать прокси заново под текущий таймлайн' : 'Прокси уже соответствует таймлайну'}
        >
          Пересобрать
        </button>
      </div>
    </div>
  )
}
