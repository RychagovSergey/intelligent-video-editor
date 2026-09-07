import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { Thumb } from './Thumb'
import { formatDuration } from '../format'
import type { AnalysisStatus, MediaFile } from '../api/types'

/** Короткая подпись на значке; полная — в подсказке, иначе имя файла не помещается. */
const STATUS_BADGE: Record<AnalysisStatus, string> = {
  pending: 'нет meta',
  analyzed: 'meta',
  stale: 'устарел',
  failed: 'ошибка',
  corrupted: 'битый',
}

const STATUS_LABEL: Record<AnalysisStatus, string> = {
  pending: 'не проанализирован',
  analyzed: 'метаданные есть',
  stale: 'файл изменён после анализа',
  failed: 'ошибка анализа',
  corrupted: 'файл не читается',
}

interface Props {
  folderId: number | null
  recursive: boolean
  query: string
  selectedId: number | null
  selectedIds: Set<number>
  onSelect: (file: MediaFile, event: React.MouseEvent) => void
  reloadKey: number
  onFilesLoaded: (files: MediaFile[]) => void
}

export function FileList({
  folderId, recursive, query, selectedId, selectedIds, onSelect, reloadKey, onFilesLoaded,
}: Props) {
  const [files, setFiles] = useState<MediaFile[]>([])
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)

  // Колбэк держим в ref: иначе его новая идентичность на каждом рендере
  // родителя перезапускала бы эффект — и запросы шли бы по кругу.
  const notify = useRef(onFilesLoaded)
  notify.current = onFilesLoaded

  useEffect(() => {
    let cancelled = false
    api.media({ folderId: folderId ?? undefined, recursive, q: query || undefined })
      .then((res) => {
        if (cancelled) return
        setFiles(res.items)
        setTotal(res.total)
        setError(null)
        notify.current(res.items)
      })
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [folderId, recursive, query, reloadKey])

  if (error) return <div className="empty error-text">{error}</div>
  if (files.length === 0) {
    return <div className="empty">{folderId == null ? 'Выберите папку слева' : 'В папке нет медиафайлов'}</div>
  }

  return (
    <div className="filelist">
      {files.map((f) => (
        <div
          key={f.id}
          className={`file-row${selectedId === f.id ? ' selected' : ''}${selectedIds.has(f.id) ? ' picked' : ''}`}
          draggable
          onDragStart={(e) => {
            // Перетаскивание файла из хранилища на таймлайн (ТЗ п. 3.3).
            e.dataTransfer.setData('application/x-media-id', String(f.id))
            e.dataTransfer.effectAllowed = 'copy'
          }}
          onClick={(e) => onSelect(f, e)}
          title={f.path}
        >
          <Thumb fileId={f.id} type={f.type} hasThumbnail={f.type !== 'audio' || f.has_cover_art} />
          <span className="file-main">
            <span className="file-name">{f.filename}</span>
            <span className="file-tech">
              {f.duration != null && <>{formatDuration(f.duration)} · </>}
              {f.width != null && <>{f.width}×{f.height}</>}
              {f.fps != null && <> · {Math.round(f.fps)} fps</>}
              {f.type === 'audio' && f.audio_codec && <>{f.audio_codec}</>}
            </span>
          </span>
          <span
            className={`badge ${f.analysis_status}`}
            title={f.probe_error ?? STATUS_LABEL[f.analysis_status]}
          >
            {STATUS_BADGE[f.analysis_status]}
          </span>
        </div>
      ))}
      {files.length < total && (
        <div className="empty">Показаны первые {files.length} из {total}</div>
      )}
    </div>
  )
}
