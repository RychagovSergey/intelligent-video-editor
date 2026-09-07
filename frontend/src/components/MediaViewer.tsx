import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { formatDuration, formatSize } from '../format'
import type { MediaFile } from '../api/types'

/** Просмотр выбранного файла хранилища: фото, видео или звук (ТЗ п. 3.7). */
export function MediaViewer({ file }: { file: MediaFile | null }) {
  const [native, setNative] = useState<boolean | null>(null)
  const [failed, setFailed] = useState(false)
  const [note, setNote] = useState<string | null>(null)

  useEffect(() => {
    setNative(null)
    setFailed(false)
    setNote(null)
    if (!file) return
    let cancelled = false
    api.playable(file.id)
      .then((info) => { if (!cancelled) setNative(info.native) })
      .catch(() => { if (!cancelled) setNative(false) })
    return () => { cancelled = true }
  }, [file])

  if (!file) return null

  const openInFfplay = () => {
    api.playInFfplay(file.id)
      .then(() => setNote('Открыто отдельным окном ffplay'))
      .catch((e: Error) => setNote(e.message))
  }

  const url = api.mediaFileUrl(file.id)
  const canPlayHere = native !== false && !failed

  return (
    <div className="viewer">
      <div className="viewer-stage">
        {file.type === 'image' && (
          <img
            className="viewer-image"
            // Формат, чуждый браузеру (HEIC и подобные), показываем перекодированной копией.
            src={canPlayHere ? url : api.thumbnailUrl(file.id, 1280)}
            alt={file.filename}
            onError={() => setFailed(true)}
          />
        )}

        {file.type === 'video' && (canPlayHere ? (
          <video className="viewer-video" src={url} controls autoPlay={false} onError={() => setFailed(true)} />
        ) : (
          <UnsupportedFormat file={file} onOpen={openInFfplay} />
        ))}

        {file.type === 'audio' && (
          <div className="viewer-audio">
            {/* Обложка есть не у всех треков — иначе запрос миниатюры вернёт 404. */}
            {file.has_cover_art
              ? <img className="viewer-cover" src={api.thumbnailUrl(file.id, 640)} alt="" />
              : <div className="viewer-cover cover-stub">♪</div>}
            {canPlayHere
              ? <audio src={url} controls onError={() => setFailed(true)} />
              : <UnsupportedFormat file={file} onOpen={openInFfplay} />}
          </div>
        )}
      </div>

      <div className="viewer-bar">
        <span className="viewer-tech">
          {file.duration != null && `${formatDuration(file.duration)} · `}
          {formatSize(file.size)}
        </span>
        <span className="spacer" />
        {note && <span className="viewer-tech">{note}</span>}
        <button onClick={openInFfplay} title="Открыть исходник в отдельном окне ffplay">
          Во весь экран
        </button>
      </div>
    </div>
  )
}

function UnsupportedFormat({ file, onOpen }: { file: MediaFile; onOpen: () => void }) {
  return (
    <div className="placeholder" style={{ flex: 1, margin: 0, border: 'none' }}>
      <div>Браузер не показывает {file.filename.split('.').pop()?.toUpperCase()}</div>
      <button className="primary" style={{ marginTop: 6 }} onClick={onOpen}>Открыть в ffplay</button>
    </div>
  )
}
