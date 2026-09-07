import { useState } from 'react'
import { api } from '../api/client'
import type { ExportStatus } from '../api/types'

const CONTAINERS = [
  { value: 'mp4', label: 'MP4 · H.264' },
  { value: 'mov', label: 'MOV · ProRes' },
  { value: 'webm', label: 'WebM · VP9' },
] as const

const QUALITIES = [
  { value: 'high', label: 'Высокое (CRF 18)' },
  { value: 'medium', label: 'Среднее (CRF 23)' },
  { value: 'low', label: 'Низкое (CRF 28)' },
] as const

interface Props {
  projectId: number
  projectName: string
  onClose: () => void
  onStarted: (status: ExportStatus) => void
}

/** Диалог экспорта: формат, качество, папка (ТЗ п. 3.6). */
export function ExportDialog({ projectId, projectName, onClose, onStarted }: Props) {
  const [directory, setDirectory] = useState('~/Movies')
  const [filename, setFilename] = useState(projectName)
  const [container, setContainer] = useState<'mp4' | 'mov' | 'webm'>('mp4')
  const [quality, setQuality] = useState<'high' | 'medium' | 'low'>('medium')
  const [bitrate, setBitrate] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const start = async () => {
    setBusy(true)
    try {
      const status = await api.export(projectId, {
        directory,
        filename,
        container,
        quality,
        bitrate: bitrate.trim() || null,
      })
      onStarted(status)
      onClose()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Экспорт проекта</h2>

        <label className="field">
          <span>Имя файла</span>
          <input type="text" value={filename} onChange={(e) => setFilename(e.target.value)} />
        </label>

        <label className="field">
          <span>Папка назначения</span>
          <input
            type="text"
            value={directory}
            onChange={(e) => setDirectory(e.target.value)}
            placeholder="~/Movies"
          />
        </label>

        <label className="field">
          <span>Формат</span>
          <select value={container} onChange={(e) => setContainer(e.target.value as typeof container)}>
            {CONTAINERS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
          </select>
        </label>

        <label className="field">
          <span>Качество</span>
          <select
            value={quality}
            disabled={container === 'mov' || bitrate.trim() !== ''}
            onChange={(e) => setQuality(e.target.value as typeof quality)}
          >
            {QUALITIES.map((q) => <option key={q.value} value={q.value}>{q.label}</option>)}
          </select>
        </label>

        {container !== 'mov' && (
          <label className="field">
            <span>Битрейт (необязательно)</span>
            <input
              type="text"
              value={bitrate}
              onChange={(e) => setBitrate(e.target.value)}
              placeholder="например 8M — заменит качество"
            />
          </label>
        )}

        {container === 'mov' && (
          <div className="hint">ProRes пишется без CRF: качество задаёт сам кодек, файл будет крупным.</div>
        )}

        {error && <div className="error-text">{error}</div>}

        <div className="modal-actions">
          <button onClick={onClose}>Отмена</button>
          <button className="primary" onClick={start} disabled={busy || !filename.trim()}>
            {busy ? 'Запускаю…' : 'Экспортировать'}
          </button>
        </div>
      </div>
    </div>
  )
}
