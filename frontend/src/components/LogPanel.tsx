import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { LogEntry } from '../api/types'

const POLL_MS = 1500
const KEEP = 1000

interface Props {
  open: boolean
  onClose: () => void
}

/** Нижняя панель логов backend (ТЗ п. 3.7): дочитывает журнал по порядковому номеру. */
export function LogPanel({ open, onClose }: Props) {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [warningsOnly, setWarningsOnly] = useState(false)
  const [follow, setFollow] = useState(true)
  const lastSeq = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const pull = async () => {
      const out = await api.logs(lastSeq.current).catch(() => null)
      if (!out || cancelled) return
      if (out.last_seq < lastSeq.current) {
        // Backend перезапустился — нумерация началась заново.
        lastSeq.current = 0
        setEntries([])
      }
      if (out.entries.length > 0) {
        lastSeq.current = out.entries[out.entries.length - 1].seq
        setEntries((prev) => [...prev, ...out.entries].slice(-KEEP))
      }
    }
    void pull()
    const timer = window.setInterval(pull, POLL_MS)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [open])

  useEffect(() => {
    if (follow && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [entries, follow, open])

  if (!open) return null

  const shown = warningsOnly ? entries.filter((e) => e.level !== 'INFO' && e.level !== 'DEBUG') : entries

  return (
    <div className="log-panel">
      <div className="pane-header">
        Журнал
        <label className="toggle">
          <input type="checkbox" checked={warningsOnly} onChange={(e) => setWarningsOnly(e.target.checked)} />
          только предупреждения
        </label>
        <label className="toggle">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          следить
        </label>
        <button style={{ padding: '1px 8px' }} onClick={() => setEntries([])}>Очистить</button>
        <button style={{ marginLeft: 'auto', padding: '1px 8px' }} onClick={onClose} title="Скрыть журнал">✕</button>
      </div>
      <div
        className="log-body"
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget
          // Прокрутили вверх — перестаём прыгать к концу, пока не вернутся вниз.
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 8)
        }}
      >
        {shown.length === 0 && <div className="empty">Записей пока нет</div>}
        {shown.map((e) => (
          <div key={e.seq} className={`log-line log-${e.level.toLowerCase()}`}>
            <span className="log-ts">{new Date(e.ts).toLocaleTimeString('ru-RU')}</span>
            <span className="log-level">{e.level}</span>
            <span className="log-logger" title={e.logger}>{e.logger.replace(/^backend\.app\./, '')}</span>
            <span className="log-msg">{e.message}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
