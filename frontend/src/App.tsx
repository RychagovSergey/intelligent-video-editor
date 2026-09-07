import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api/client'
import { FileList } from './components/FileList'
import { MediaTree } from './components/MediaTree'
import { MetaPanel } from './components/MetaPanel'
import { AgentPanel } from './components/AgentPanel'
import { ExportDialog } from './components/ExportDialog'
import { PreviewPlayer } from './components/PreviewPlayer'
import { StatusBar } from './components/StatusBar'
import { Timeline } from './components/Timeline'
import type {
  AgentInfo, AgentStatus, AnalyzeStatus, ExportStatus, Folder, Health, MediaFile,
  ModelsInfo, PreviewStatus, ProjectInfo, ScanStatus, StorageStats, TimelineState,
} from './api/types'

const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4]
const FADES = [0, 0.3, 0.5, 1, 1.5, 2]

const IDLE_ANALYZE: AnalyzeStatus = {
  state: 'idle', model: null, current_file: null, current_step: null,
  done: 0, total: 0, analyzed: 0, failed: 0, skipped: 0, errors: [], error: null,
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [backendError, setBackendError] = useState<string | null>(null)
  const [stats, setStats] = useState<StorageStats | null>(null)
  const [scan, setScan] = useState<ScanStatus | null>(null)
  const [analyze, setAnalyze] = useState<AnalyzeStatus | null>(null)
  const [models, setModels] = useState<ModelsInfo | null>(null)
  const [pending, setPending] = useState(0)

  const [folder, setFolder] = useState<Folder | null>(null)
  const [file, setFile] = useState<MediaFile | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [visibleFiles, setVisibleFiles] = useState<MediaFile[]>([])
  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [projectId, setProjectId] = useState<number | null>(null)
  const [timeline, setTimeline] = useState<TimelineState | null>(null)
  const [selectedClip, setSelectedClip] = useState<string | null>(null)
  const [playhead, setPlayhead] = useState(0)
  const [timelineError, setTimelineError] = useState<string | null>(null)
  const [preview, setPreview] = useState<PreviewStatus | null>(null)
  const [exportState, setExportState] = useState<ExportStatus | null>(null)
  const [exportOpen, setExportOpen] = useState(false)
  const [agentInfo, setAgentInfo] = useState<AgentInfo | null>(null)
  const [agent, setAgent] = useState<AgentStatus | null>(null)

  const [query, setQuery] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [fastModel, setFastModel] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  const scanTimer = useRef<number | null>(null)
  const previewTimer = useRef<number | null>(null)
  const exportTimer = useRef<number | null>(null)
  const analyzeTimer = useRef<number | null>(null)

  const refreshCounters = useCallback((folderId?: number) => {
    api.stats().then(setStats).catch(() => undefined)
    api.estimate(folderId).then((e) => setPending(e.pending_total)).catch(() => undefined)
  }, [])

  useEffect(() => {
    api.health()
      .then((h) => { setHealth(h); setBackendError(null) })
      .catch((e: Error) => setBackendError(e.message))
    api.scanStatus().then(setScan).catch(() => undefined)
    api.analyzeStatus().then(setAnalyze).catch(() => undefined)
    api.models().then(setModels).catch(() => undefined)
    api.agentInfo().then(setAgentInfo).catch(() => undefined)
    api.agentStatus().then(setAgent).catch(() => undefined)
    api.projects().then((list) => {
      setProjects(list)
      if (list.length > 0) setProjectId(list[0].id)   // открываем последний правленный
    }).catch(() => undefined)
    refreshCounters()
  }, [refreshCounters])

  // Выбрали другую папку — счётчик на кнопке «Анализ всего» считается для неё.
  useEffect(() => {
    refreshCounters(folder?.id)
  }, [folder?.id, refreshCounters])

  useEffect(() => {
    if (projectId == null) { setTimeline(null); return }
    api.timeline(projectId)
      .then((state) => { setTimeline(state); setTimelineError(null) })
      .catch((e: Error) => setTimelineError(e.message))
  }, [projectId])

  // Опрос состояния скана; на этапе 8 заменится на WebSocket.
  useEffect(() => {
    if (scan?.state !== 'running') {
      if (scanTimer.current) { window.clearInterval(scanTimer.current); scanTimer.current = null }
      return
    }
    scanTimer.current = window.setInterval(async () => {
      const s = await api.scanStatus().catch(() => null)
      if (!s) return
      setScan(s)
      if (s.state !== 'running') { setReloadKey((k) => k + 1); refreshCounters(folder?.id) }
    }, 500)
    return () => { if (scanTimer.current) window.clearInterval(scanTimer.current) }
  }, [scan?.state, refreshCounters, folder?.id])

  useEffect(() => {
    if (analyze?.state !== 'running') {
      if (analyzeTimer.current) { window.clearInterval(analyzeTimer.current); analyzeTimer.current = null }
      return
    }
    let lastDone = -1
    analyzeTimer.current = window.setInterval(async () => {
      const a = await api.analyzeStatus().catch(() => null)
      if (!a) return
      setAnalyze(a)
      // Список перечитываем только когда очередной файл разобран,
      // а не на каждый опрос состояния.
      if (a.done !== lastDone) {
        lastDone = a.done
        setReloadKey((k) => k + 1)
      }
      if (a.state !== 'running') refreshCounters(folder?.id)
    }, 1500)
    return () => { if (analyzeTimer.current) window.clearInterval(analyzeTimer.current) }
  }, [analyze?.state, refreshCounters, folder?.id])

  const startScan = async () => {
    try { setScan(await api.startScan()) } catch (e) {
      setScan({
        state: 'error', phase: null, started_at: null, finished_at: null,
        current_root: null, current_file: null, done: 0, total: 0,
        stats: null, error: (e as Error).message,
      })
    }
  }

  const startAnalysis = async (scope: 'selection' | 'folder' | 'all') => {
    // «Выбранное» разбираем заново даже при готовом meta.json, «всё» — только без метаданных.
    const body =
      scope === 'selection' ? { file_ids: [...selectedIds], force: true }
      // «Всё» — это выбранная папка с подпапками; без выбора — всё хранилище.
      : folder != null ? { folder_id: folder.id, recursive: true }
      : {}
    try {
      setAnalyze(await api.analyze({ ...body, fast: fastModel }))
    } catch (e) {
      setAnalyze({ ...IDLE_ANALYZE, state: 'error', error: (e as Error).message })
    }
  }

  // Пока прокси собирается и играет, следим за состоянием.
  useEffect(() => {
    const active = preview?.state === 'rendering' || preview?.state === 'playing'
    if (!active) {
      if (previewTimer.current) { window.clearInterval(previewTimer.current); previewTimer.current = null }
      return
    }
    previewTimer.current = window.setInterval(async () => {
      const p = await api.previewStatus().catch(() => null)
      if (!p) return
      setPreview(p)
      if (p.state !== 'rendering' && p.state !== 'playing' && projectId != null) {
        // Прокси собран — перечитываем таймлайн, чтобы снялась пометка «устарело».
        api.timeline(projectId).then(setTimeline).catch(() => undefined)
      }
    }, 700)
    return () => { if (previewTimer.current) window.clearInterval(previewTimer.current) }
  }, [preview?.state, projectId])

  useEffect(() => {
    if (exportState?.state !== 'running') {
      if (exportTimer.current) { window.clearInterval(exportTimer.current); exportTimer.current = null }
      return
    }
    exportTimer.current = window.setInterval(async () => {
      const s = await api.exportStatus().catch(() => null)
      if (s) setExportState(s)
    }, 800)
    return () => { if (exportTimer.current) window.clearInterval(exportTimer.current) }
  }, [exportState?.state])

  const startPreview = async (player: 'embedded' | 'ffplay' = 'embedded') => {
    if (projectId == null) return
    try {
      setPreview(await api.preview(projectId, player))
      setTimelineError(null)
    } catch (e) {
      setTimelineError((e as Error).message)
    }
  }

  const createProject = async () => {
    const name = window.prompt('Название проекта', `Монтаж ${new Date().toLocaleDateString('ru-RU')}`)
    if (name === null) return
    const project = await api.createProject(name).catch((e: Error) => {
      setTimelineError(e.message)
      return null
    })
    if (!project) return
    setProjects((prev) => [project, ...prev])
    setProjectId(project.id)
    setSelectedClip(null)
    setPlayhead(0)
  }

  /** Перечитать проект: внешний агент по MCP правит базу мимо интерфейса. */
  const refreshProject = async () => {
    try {
      const [list, state] = await Promise.all([
        api.projects(),
        projectId != null ? api.timeline(projectId) : Promise.resolve(null),
      ])
      setProjects(list)
      if (state) setTimeline(state)
      setTimelineError(null)
      refreshCounters(folder?.id)
      setReloadKey((k) => k + 1)
    } catch (e) {
      setTimelineError((e as Error).message)
    }
  }

  const deleteProject = async () => {
    if (projectId == null) return
    const name = projects.find((p) => p.id === projectId)?.name ?? 'проект'
    if (!window.confirm(`Удалить проект «${name}»? Медиафайлы останутся на месте, монтаж будет потерян.`)) return
    try {
      await api.deleteProject(projectId)
      const rest = projects.filter((p) => p.id !== projectId)
      setProjects(rest)
      setProjectId(rest.length > 0 ? rest[0].id : null)
      setSelectedClip(null)
      setPlayhead(0)
      setPreview(null)
      setTimelineError(null)
    } catch (e) {
      setTimelineError((e as Error).message)
    }
  }

  const runOnTimeline = async (action: () => Promise<TimelineState>) => {
    try {
      setTimeline(await action())
      setTimelineError(null)
    } catch (e) {
      setTimelineError((e as Error).message)
    }
  }

  const clipById = timeline?.timeline.tracks
    .flatMap((t) => t.clips)
    .find((c) => c.id === selectedClip) ?? null

  const addSelectionToTimeline = async () => {
    if (projectId == null) return
    for (const id of selectedIds) {
      await runOnTimeline(() => api.addClip(projectId, { source_id: id }))
    }
  }

  // Горячие клавиши таймлайна: пока фокус не в поле ввода.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || projectId == null) return

      if (event.code === 'Space') {
        event.preventDefault()
        const video = document.querySelector<HTMLVideoElement>('.player-video')
        if (video) video.paused ? void video.play() : video.pause()
      } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault()
        void runOnTimeline(() => (event.shiftKey ? api.redo(projectId) : api.undo(projectId)))
      } else if ((event.key === 'Delete' || event.key === 'Backspace') && selectedClip) {
        event.preventDefault()
        void runOnTimeline(() => api.deleteClip(projectId, selectedClip))
        setSelectedClip(null)
      } else if (event.key.toLowerCase() === 's' && selectedClip) {
        event.preventDefault()
        void runOnTimeline(() => api.splitClip(projectId, selectedClip, playhead))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [projectId, selectedClip, playhead])

  const cancelAnalysis = async () => {
    setAnalyze(await api.cancelAnalysis().catch(() => analyze ?? IDLE_ANALYZE))
  }

  /** Обычный клик — выбрать один файл, cmd/ctrl — добавить в набор, shift — диапазон. */
  const pickFile = (clicked: MediaFile, event: React.MouseEvent) => {
    setFile(clicked)
    setSelectedIds((prev) => {
      if (event.metaKey || event.ctrlKey) {
        const next = new Set(prev)
        next.has(clicked.id) ? next.delete(clicked.id) : next.add(clicked.id)
        return next
      }
      if (event.shiftKey && file) {
        const from = visibleFiles.findIndex((f) => f.id === file.id)
        const to = visibleFiles.findIndex((f) => f.id === clicked.id)
        if (from >= 0 && to >= 0) {
          const [a, b] = from < to ? [from, to] : [to, from]
          return new Set(visibleFiles.slice(a, b + 1).map((f) => f.id))
        }
      }
      return new Set([clicked.id])
    })
  }

  const previewBusy = preview?.state === 'rendering' || preview?.state === 'playing'
  // Прокси собран именно для текущего состояния таймлайна — пересобирать нечего.
  const previewFresh = timeline?.preview_ready === true
  const scanning = scan?.state === 'running'
  const analyzing = analyze?.state === 'running'
  const busy = scanning || analyzing
  const fastMissing = models?.fast_installed === false

  return (
    <div className="app">
      <header className="topbar">
        <span className="title">Intelligent Video Editor</span>
        <button onClick={startScan} disabled={busy} className="primary">
          {scanning ? 'Сканирую…' : 'Сканировать'}
        </button>
        <button
          onClick={() => startAnalysis('selection')}
          disabled={busy || selectedIds.size === 0}
          title="Разобрать отмеченные файлы заново, даже если meta.json уже есть (cmd+клик — несколько)"
        >
          Анализ выбранного{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}
        </button>
        <button
          onClick={() => startAnalysis('all')}
          disabled={busy || pending === 0}
          title={folder
            ? `Разобрать файлы без метаданных в папке «${folder.name}» и её подпапках`
            : 'Разобрать все файлы хранилища без метаданных'}
        >
          {folder ? `Анализ папки` : 'Анализ всего'}{pending > 0 ? ` (${pending})` : ''}
        </button>
        <label className="toggle" title={fastMissing ? `Модель ${models?.fast} не установлена` : 'Быстрая VL-модель'}>
          <input
            type="checkbox"
            checked={fastModel}
            disabled={fastMissing}
            onChange={(e) => setFastModel(e.target.checked)}
          />
          быстрая модель
        </label>
        <span className="divider" />
        <select
          value={projectId ?? ''}
          onChange={(e) => setProjectId(e.target.value ? Number(e.target.value) : null)}
          title="Открыть проект"
        >
          <option value="">— проект не выбран —</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button
          onClick={refreshProject}
          title="Перечитать проект и хранилище — например, после работы внешнего агента по MCP"
        >
          ↻
        </button>
        <button onClick={createProject}>Новый проект</button>
        <button
          onClick={deleteProject}
          disabled={projectId == null || busy || exportState?.state === 'running'}
          title="Удалить открытый проект (медиафайлы не трогаются)"
        >
          Удалить проект
        </button>
        <button
          onClick={() => startPreview('embedded')}
          disabled={projectId == null || !timeline || timeline.duration <= 0 || previewBusy || previewFresh}
          title={previewFresh
            ? 'Превью уже собрано для текущего состояния таймлайна'
            : 'Собрать прокси для текущего состояния таймлайна'}
        >
          {preview?.state === 'rendering'
            ? 'Готовлю…'
            : previewFresh ? 'Превью актуально' : timeline?.preview_ready === false && preview
              ? 'Обновить превью'
              : 'Собрать превью'}
        </button>
        <button
          onClick={() => startPreview('ffplay')}
          disabled={projectId == null || !timeline || timeline.duration <= 0 || previewBusy}
          title="Открыть отдельным окном ffplay"
        >
          ffplay
        </button>
        <button
          onClick={() => setExportOpen(true)}
          disabled={projectId == null || !timeline || timeline.duration <= 0 || exportState?.state === 'running'}
          title="Собрать итоговый файл"
        >
          {exportState?.state === 'running' ? 'Экспортирую…' : 'Экспорт'}
        </button>
        <span className="spacer" />
        <input
          type="search"
          placeholder="Поиск по имени файла"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ width: 200 }}
        />
      </header>

      <div className="main">
        <section className="pane">
          <div className="pane-header">Хранилище</div>
          <div className="pane-body" style={{ flex: '0 0 34%', borderBottom: '1px solid var(--line)' }}>
            <MediaTree
              selectedId={folder?.id ?? null}
              onSelect={(f) => { setFolder(f); setFile(null); setSelectedIds(new Set()) }}
              reloadKey={reloadKey}
            />
          </div>
          <div className="pane-header">
            Файлы
            <label className="toggle" style={{ marginLeft: 'auto' }}>
              <input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} />
              с подпапками
            </label>
          </div>
          <div className="pane-body">
            <FileList
              folderId={folder?.id ?? null}
              recursive={recursive}
              query={query}
              selectedId={file?.id ?? null}
              selectedIds={selectedIds}
              onSelect={pickFile}
              reloadKey={reloadKey}
              onFilesLoaded={setVisibleFiles}
            />
          </div>
        </section>

        <section className="pane pane-center">
          <div className="preview-area">
            <PreviewPlayer
              projectId={projectId}
              preview={preview}
              stale={timeline != null && timeline.preview_ready === false}
              playhead={playhead}
              onPlayhead={setPlayhead}
              onRequestRender={() => startPreview('embedded')}
            />
          </div>
          <div className="timeline-area">
            {projectId != null && (
              <div className="clip-toolbar">
                <button
                  onClick={() => addSelectionToTimeline()}
                  disabled={selectedIds.size === 0}
                  title="Добавить отмеченные файлы в конец дорожки"
                >
                  Добавить в проект{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}
                </button>
                <span className="divider" />
                <button
                  onClick={() => runOnTimeline(() => api.splitClip(projectId, selectedClip!, playhead))}
                  disabled={!selectedClip}
                  title="Разрезать клип по курсору (S)"
                >
                  Разрезать
                </button>
                <button
                  onClick={() => { runOnTimeline(() => api.deleteClip(projectId, selectedClip!)); setSelectedClip(null) }}
                  disabled={!selectedClip}
                  title="Удалить клип (Delete)"
                >
                  Удалить
                </button>
                <button
                  onClick={() => runOnTimeline(() => api.muteClip(projectId, selectedClip!, !clipById?.muted))}
                  disabled={!selectedClip}
                  title="Отключить собственный звук клипа, чтобы он не наслаивался на музыку"
                >
                  {clipById?.muted ? '🔇 без звука' : '🔊 со звуком'}
                </button>
                <label className="toggle">
                  скорость
                  <select
                    value={clipById?.speed ?? 1}
                    disabled={!selectedClip}
                    onChange={(e) => runOnTimeline(() => api.setClipSpeed(projectId, selectedClip!, Number(e.target.value)))}
                  >
                    {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
                  </select>
                </label>
                <label className="toggle" title="Плавное появление клипа">
                  fade in
                  <select
                    value={clipById?.fade_in ?? 0}
                    disabled={!selectedClip}
                    onChange={(e) => runOnTimeline(() => api.setClipFade(projectId, selectedClip!, { fade_in: Number(e.target.value) }))}
                  >
                    {FADES.map((s) => <option key={s} value={s}>{s}с</option>)}
                  </select>
                </label>
                <label className="toggle" title="Плавное затухание клипа">
                  fade out
                  <select
                    value={clipById?.fade_out ?? 0}
                    disabled={!selectedClip}
                    onChange={(e) => runOnTimeline(() => api.setClipFade(projectId, selectedClip!, { fade_out: Number(e.target.value) }))}
                  >
                    {FADES.map((s) => <option key={s} value={s}>{s}с</option>)}
                  </select>
                </label>
                <button
                  onClick={() => runOnTimeline(() => api.closeGaps(projectId, 'video_1'))}
                  title="Сдвинуть клипы видеодорожки встык"
                >
                  Убрать пустоты
                </button>
                <span className="divider" />
                <button onClick={() => runOnTimeline(() => api.undo(projectId))} disabled={!timeline?.can_undo} title="Отменить (⌘Z)">↶</button>
                <button onClick={() => runOnTimeline(() => api.redo(projectId))} disabled={!timeline?.can_redo} title="Повторить (⌘⇧Z)">↷</button>
                {timelineError && <span className="error-text">{timelineError}</span>}
              </div>
            )}
            <Timeline
              state={timeline}
              selectedClipId={selectedClip}
              onSelectClip={setSelectedClip}
              onChange={(next) => { setTimeline(next); setTimelineError(null) }}
              onError={setTimelineError}
              playhead={playhead}
              onPlayhead={setPlayhead}
            />
          </div>
        </section>

        <section className="pane">
          <div className="pane-header">
            Метаданные
            {file && (
              <button
                style={{ marginLeft: 'auto', padding: '2px 8px' }}
                disabled={busy}
                onClick={() => api.analyze({ file_ids: [file.id], force: true, fast: fastModel })
                  .then(setAnalyze)
                  .catch((e: Error) => setAnalyze({ ...IDLE_ANALYZE, state: 'error', error: e.message }))}
                title="Перезаписать meta.json для этого файла"
              >
                Переанализировать
              </button>
            )}
          </div>
          <div className="pane-body">
            <MetaPanel file={file} reloadKey={reloadKey} />
          </div>
        </section>
      </div>

      {exportOpen && projectId != null && (
        <ExportDialog
          projectId={projectId}
          projectName={projects.find((p) => p.id === projectId)?.name ?? 'export'}
          onClose={() => setExportOpen(false)}
          onStarted={setExportState}
        />
      )}

      <AgentPanel
        projectId={projectId}
        info={agentInfo}
        status={agent}
        onStatus={setAgent}
        onFinished={() => {
          // Агент правил таймлайн — перечитываем проект и снимаем выделение клипа.
          if (projectId != null) api.timeline(projectId).then(setTimeline).catch(() => undefined)
          setSelectedClip(null)
        }}
      />

      <StatusBar
        health={health}
        scan={scan}
        analyze={analyze}
        preview={preview}
        stats={stats}
        backendError={backendError}
        onCancelAnalysis={cancelAnalysis}
        onCancelPreview={() => api.cancelPreview().then(setPreview).catch(() => undefined)}
        exportState={exportState}
        onCancelExport={() => api.cancelExport().then(setExportState).catch(() => undefined)}
        onRevealExport={() => api.revealExport().catch(() => undefined)}
      />
    </div>
  )
}
