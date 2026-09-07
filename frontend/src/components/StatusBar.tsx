import { formatDuration, formatSize } from '../format'
import type {
  AnalyzeStatus, ExportStatus, Health, PreviewStatus, ScanStatus, StorageStats,
} from '../api/types'

interface Props {
  health: Health | null
  scan: ScanStatus | null
  analyze: AnalyzeStatus | null
  preview: PreviewStatus | null
  stats: StorageStats | null
  backendError: string | null
  exportState: ExportStatus | null
  onCancelAnalysis: () => void
  onCancelPreview: () => void
  onCancelExport: () => void
  onRevealExport: () => void
}

export function StatusBar({
  health, scan, analyze, preview, exportState, stats, backendError,
  onCancelAnalysis, onCancelPreview, onCancelExport, onRevealExport,
}: Props) {
  const scanning = scan?.state === 'running'
  const analyzing = analyze?.state === 'running'
  const previewing = preview?.state === 'rendering' || preview?.state === 'playing'
  const dot = backendError ? 'err' : scanning || analyzing || previewing ? 'busy' : 'ok'

  let scanText = ''
  let progress: number | null = null
  if (scanning) {
    if (scan?.phase === 'probing') {
      scanText = `Характеристики: ${scan.done}/${scan.total}${scan.current_file ? ` · ${scan.current_file}` : ''}`
      progress = scan.total > 0 ? scan.done / scan.total : null
    } else {
      scanText = `Сканирую: ${scan?.current_root ?? '…'}`
    }
  } else if (scan?.state === 'done' && scan.stats) {
    const s = scan.stats
    scanText = `Готово: файлов ${s.files_total}, новых ${s.files_added}, изменённых ${s.files_updated}, meta ${s.meta_loaded}`
    if (s.files_missing) scanText += `, пропало ${s.files_missing}`
    if (s.corrupted) scanText += `, повреждено ${s.corrupted}`
  } else if (scan?.state === 'error') {
    scanText = `Ошибка скана: ${scan.error}`
  }

  const pending = stats?.by_status?.pending ?? 0

  return (
    <div className="statusbar">
      <span>
        <span className={`dot ${dot}`} />
        {backendError ? <span className="error-text">Backend недоступен: {backendError}</span> : 'Backend'}
      </span>
      {health && (
        <span title="ffmpeg / ffprobe / ffplay">
          ffmpeg {health.ffmpeg ? '✓' : <span className="error-text">не найден</span>}
        </span>
      )}
      {health && <span title="Модели анализа (Р-9)">VL: {health.vl_model} / {health.vl_model_fast}</span>}
      {health && (
        <span title="Провайдер агента (Р-10)">
          Агент: {health.agent_provider} {health.agent_configured ? '✓' : <span className="error-text">нет ключа</span>}
        </span>
      )}
      {stats && (
        <span>Файлов: {stats.total}{pending > 0 && ` · не проанализировано: ${pending}`}</span>
      )}
      <span className="spacer" />
      {exportState && exportState.state !== 'idle' && (
        <ExportProgress state={exportState} onCancel={onCancelExport} onReveal={onRevealExport} />
      )}
      {preview && preview.state !== 'idle' && (
        <PreviewProgress preview={preview} onCancel={onCancelPreview} />
      )}
      {analyze && analyze.state !== 'idle' && (
        <AnalyzeProgress analyze={analyze} onCancel={onCancelAnalysis} />
      )}
      {progress != null && (
        <span className="progress"><i style={{ width: `${Math.round(progress * 100)}%` }} /></span>
      )}
      <span className={scan?.state === 'error' ? 'error-text' : ''}>{scanText}</span>
    </div>
  )
}


function AnalyzeProgress({ analyze, onCancel }: { analyze: AnalyzeStatus; onCancel: () => void }) {
  if (analyze.state === 'running') {
    const ratio = analyze.total > 0 ? analyze.done / analyze.total : 0
    return (
      <>
        <span className="progress"><i style={{ width: `${Math.round(ratio * 100)}%` }} /></span>
        <span title={analyze.current_file ?? ''}>
          Анализ {analyze.done}/{analyze.total}
          {analyze.current_file && ` · ${analyze.current_file}`}
          {analyze.current_step && ` · ${analyze.current_step}`}
        </span>
        <button onClick={onCancel}>Остановить</button>
      </>
    )
  }

  if (analyze.state === 'error') {
    return <span className="error-text">Анализ: {analyze.error}</span>
  }

  const tail = analyze.state === 'cancelled' ? 'Анализ остановлен' : 'Анализ завершён'
  return (
    <span
      className={analyze.failed > 0 ? 'error-text' : ''}
      title={analyze.errors.join('\n')}
    >
      {tail}: разобрано {analyze.analyzed}
      {analyze.failed > 0 && `, с ошибкой ${analyze.failed}`}
      {analyze.skipped > 0 && `, пропущено ${analyze.skipped}`}
    </span>
  )
}


function PreviewProgress({ preview, onCancel }: { preview: PreviewStatus; onCancel: () => void }) {
  if (preview.state === 'rendering') {
    const ratio = preview.total_seconds > 0 ? preview.done_seconds / preview.total_seconds : 0
    return (
      <>
        <span className="progress"><i style={{ width: `${Math.round(ratio * 100)}%` }} /></span>
        <span>Прокси {Math.round(preview.done_seconds)}/{Math.round(preview.total_seconds)} с</span>
        <button onClick={onCancel}>Отменить</button>
      </>
    )
  }
  if (preview.state === 'ready') {
    return <span>Превью готово{preview.cached ? ' (из кеша)' : ''}</span>
  }
  if (preview.state === 'playing') {
    return (
      <>
        <span>Предпросмотр в ffplay{preview.cached ? ' (из кеша)' : ''}</span>
        <button onClick={onCancel}>Закрыть плеер</button>
      </>
    )
  }
  if (preview.state === 'error') return <span className="error-text">Предпросмотр: {preview.error}</span>
  if (preview.state === 'cancelled') return <span>Предпросмотр отменён</span>
  return null
}


function ExportProgress(
  { state, onCancel, onReveal }: { state: ExportStatus; onCancel: () => void; onReveal: () => void },
) {
  if (state.state === 'running') {
    return (
      <>
        <span className="progress"><i style={{ width: `${state.percent}%` }} /></span>
        <span>
          Экспорт {state.percent.toFixed(0)}%
          {state.eta_seconds != null && ` · осталось ${formatDuration(state.eta_seconds)}`}
        </span>
        <button onClick={onCancel}>Отменить</button>
      </>
    )
  }
  if (state.state === 'done') {
    return (
      <>
        <span>Экспорт готов{state.size_bytes ? ` · ${formatSize(state.size_bytes)}` : ''}</span>
        <button onClick={onReveal} title={state.output ?? ''}>Показать в Finder</button>
      </>
    )
  }
  if (state.state === 'error') return <span className="error-text">Экспорт: {state.error}</span>
  if (state.state === 'cancelled') return <span>Экспорт отменён</span>
  return null
}
