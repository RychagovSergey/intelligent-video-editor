import { useState } from 'react'
import { api } from '../api/client'
import type { MediaType } from '../api/types'

const FALLBACK: Record<MediaType, string> = { video: '🎬', image: '🖼', audio: '🎵' }

/** Миниатюра файла; пока её нет (или не создалась) — иконка типа. */
interface Props {
  fileId: number
  type: MediaType
  size?: number
  /** У аудио без обложки миниатюры нет — не дёргаем сервер впустую. */
  hasThumbnail?: boolean
}

export function Thumb({ fileId, type, size = 34, hasThumbnail = true }: Props) {
  const [failed, setFailed] = useState(false)

  if (failed || !hasThumbnail) {
    return <span className="thumb thumb-fallback" style={{ width: size, height: size }}>{FALLBACK[type]}</span>
  }
  return (
    <img
      className="thumb"
      style={{ width: size, height: size }}
      src={api.thumbnailUrl(fileId)}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
    />
  )
}
