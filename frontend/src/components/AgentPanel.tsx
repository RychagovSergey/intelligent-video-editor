import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AgentInfo, AgentStatus } from '../api/types'

interface Props {
  projectId: number | null
  info: AgentInfo | null
  status: AgentStatus | null
  onStatus: (status: AgentStatus) => void
  onFinished: () => void
}

/** Поле команды агенту и лог его действий — нижняя панель из макета (ТЗ п. 3.7). */
export function AgentPanel({ projectId, info, status, onStatus, onFinished }: Props) {
  const [prompt, setPrompt] = useState('')
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const running = status?.state === 'running'

  useEffect(() => {
    if (!running) {
      if (timer.current) { window.clearInterval(timer.current); timer.current = null }
      return
    }
    setOpen(true)
    timer.current = window.setInterval(async () => {
      const s = await api.agentStatus().catch(() => null)
      if (!s) return
      onStatus(s)
      if (s.state !== 'running') onFinished()
    }, 1000)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [running, onStatus, onFinished])

  const run = async () => {
    if (projectId == null || !prompt.trim()) return
    setError(null)
    try {
      onStatus(await api.runAgent(projectId, prompt.trim()))
      setOpen(true)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const disabledReason =
    projectId == null ? 'Сначала откройте проект'
    : info && !info.configured ? (info.error ?? 'Агент не настроен')
    : null

  return (
    <div className="agent-panel">
      <div className="agent-input-row">
        <textarea
          rows={1}
          placeholder={disabledReason ??
            'Например: собери динамичное видео с людьми под музыку, около 30 секунд. ' +
            'Можно подробный бриф — ⌘/Ctrl+Enter отправляет, Enter переносит строку.'}
          value={prompt}
          disabled={!!disabledReason || running}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void run() } }}
        />
        <button className="primary" onClick={run} disabled={!!disabledReason || running || !prompt.trim()}>
          {running ? `Работаю… шаг ${status?.step}/${status?.max_steps}` : 'Собрать агентом'}
        </button>
        {running && <button onClick={() => api.cancelAgent().then(onStatus).catch(() => undefined)}>Стоп</button>}
        {status && status.state !== 'idle' && (
          <button onClick={() => setOpen((v) => !v)} title="Показать, что делал агент">
            {open ? 'Скрыть лог' : 'Лог'}
          </button>
        )}
        {info?.model && <span className="agent-model">{info.provider} · {info.model}</span>}
      </div>

      {error && <div className="error-text agent-line">{error}</div>}

      {open && status && status.state !== 'idle' && (
        <div className="agent-log">
          {status.log.map((entry, i) => (
            <div key={i} className={`agent-line${entry.kind === 'error' ? ' error-text' : ''}`}>
              {entry.kind === 'answer' ? '→ ' : entry.kind === 'error' ? '✕ ' : '• '}
              {entry.text}
            </div>
          ))}
          {status.state === 'error' && <div className="agent-line error-text">✕ {status.error}</div>}
          {status.state === 'cancelled' && <div className="agent-line">Остановлено пользователем</div>}
        </div>
      )}
    </div>
  )
}
