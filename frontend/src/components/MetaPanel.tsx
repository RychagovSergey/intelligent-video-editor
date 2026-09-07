import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { MediaViewer } from './MediaViewer'
import { formatBitrate, formatDuration, formatSize } from '../format'
import type { FileMeta, MediaFile } from '../api/types'

/** Сколько элементов длинного массива показать целиком, прежде чем свернуть остальное. */
const ARRAY_PREVIEW = 6

/**
 * Сворачивает длинные массивы чисел в короткую строку.
 *
 * Тактовая сетка трека — это сотни секундных отметок (`beat_times`): развёрнутыми они
 * топят в себе всё остальное содержимое meta.json, а человеку по ним и так ничего не
 * видно. Сам JSON от этого не меняется — сворачиваем только для показа.
 */
function collapseLongArrays(value: unknown): unknown {
  if (Array.isArray(value)) {
    if (value.length > ARRAY_PREVIEW && value.every((v) => typeof v === 'number')) {
      return `[${value.length} значений: ${value.slice(0, ARRAY_PREVIEW).join(', ')}, …]`
    }
    return value.map(collapseLongArrays)
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, collapseLongArrays(v)]),
    )
  }
  return value
}

export function MetaPanel({ file, reloadKey = 0 }: { file: MediaFile | null; reloadKey?: number }) {
  const [meta, setMeta] = useState<FileMeta | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setMeta(null)
    setError(null)
    if (!file) return
    let cancelled = false
    api.meta(file.id)
      .then((m) => !cancelled && setMeta(m))
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [file, reloadKey])

  if (!file) return <div className="empty">Выберите файл, чтобы увидеть метаданные</div>

  return (
    <>
      <div className="meta-viewer">
        <MediaViewer file={file} />
      </div>
      <div className="meta-field">
        <div className="meta-key">Файл</div>
        <div className="meta-value">{file.filename}</div>
      </div>
      <div className="meta-field">
        <div className="meta-key">Путь</div>
        <div className="meta-value" style={{ wordBreak: 'break-all', color: 'var(--text-dim)' }}>{file.path}</div>
      </div>
      <div className="meta-field">
        <div className="meta-key">Тип · размер</div>
        <div className="meta-value">{file.type} · {formatSize(file.size)}</div>
      </div>
      {(file.width != null || file.duration != null) && (
        <div className="meta-field">
          <div className="meta-key">Параметры</div>
          <div className="meta-value">
            {file.width != null && <>{file.width}×{file.height}</>}
            {file.duration != null && <>{file.width != null && ' · '}{formatDuration(file.duration)}</>}
            {file.fps != null && <> · {file.fps} fps</>}
          </div>
        </div>
      )}
      {(file.video_codec || file.audio_codec) && (
        <div className="meta-field">
          <div className="meta-key">Кодеки · битрейт</div>
          <div className="meta-value">
            {[file.video_codec, file.audio_codec].filter(Boolean).join(' + ')}
            {file.bitrate != null && <> · {formatBitrate(file.bitrate)}</>}
          </div>
        </div>
      )}
      {file.probe_error && (
        <div className="meta-field">
          <div className="meta-key">ffprobe</div>
          <div className="meta-value error-text">{file.probe_error}</div>
        </div>
      )}
      <div className="meta-field">
        <div className="meta-key">Изменён</div>
        <div className="meta-value">{new Date(file.modified * 1000).toLocaleString('ru-RU')}</div>
      </div>
      {typeof meta?.meta?.bpm === 'number' && (
        <div className="meta-field">
          <div className="meta-key">Темп</div>
          <div className="meta-value">{meta.meta.bpm} BPM</div>
        </div>
      )}
      {meta?.model && (
        <div className="meta-field">
          <div className="meta-key">Модель анализа</div>
          <div className="meta-value">{meta.model}</div>
        </div>
      )}

      {error && <div className="empty error-text">{error}</div>}
      {!error && meta && (meta.meta
        ? <pre className="meta-json">{JSON.stringify(collapseLongArrays(meta.meta), null, 2)}</pre>
        : (
          <div className="empty">
            Файл ещё не проанализирован.<br />
            Анализ через Qwen2.5-VL появится на этапе 3.
          </div>
        )
      )}
    </>
  )
}
