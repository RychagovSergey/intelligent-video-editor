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

/** Поля meta.json, которые правятся руками; всё остальное — только от анализа. */
const TEXT_FIELDS: Array<{ key: string; label: string; long?: boolean }> = [
  { key: 'summary', label: 'Сводка', long: true },
  { key: 'description', label: 'Описание', long: true },
  { key: 'title_hint', label: 'Название' },
  { key: 'style', label: 'Стиль' },
  { key: 'emotions', label: 'Настроение' },
  { key: 'quality', label: 'Качество' },
]
const LIST_FIELDS: Array<{ key: string; label: string }> = [
  { key: 'objects', label: 'Объекты (через запятую)' },
  { key: 'colors', label: 'Цвета (через запятую)' },
]

interface Draft {
  fields: Record<string, string>
  scenes: Record<number, string>
}

function draftFrom(meta: Record<string, unknown>): Draft {
  const fields: Record<string, string> = {}
  for (const { key } of TEXT_FIELDS) if (key in meta) fields[key] = String(meta[key] ?? '')
  for (const { key } of LIST_FIELDS) if (key in meta) fields[key] = ((meta[key] as unknown[]) ?? []).join(', ')
  const scenes: Record<number, string> = {}
  const list = Array.isArray(meta.scenes) ? (meta.scenes as Array<Record<string, unknown>>) : []
  list.forEach((scene, i) => { scenes[i] = String(scene.description ?? '') })
  return { fields, scenes }
}

export function MetaPanel({ file, reloadKey = 0 }: { file: MediaFile | null; reloadKey?: number }) {
  const [meta, setMeta] = useState<FileMeta | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setMeta(null)
    setError(null)
    setDraft(null)
    if (!file) return
    let cancelled = false
    api.meta(file.id)
      .then((m) => !cancelled && setMeta(m))
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => { cancelled = true }
  }, [file, reloadKey])

  if (!file) return <div className="empty">Выберите файл, чтобы увидеть метаданные</div>

  const save = async () => {
    if (!draft || !meta?.meta) return
    setSaving(true)
    setError(null)
    const original = draftFrom(meta.meta)
    const fields: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(draft.fields)) {
      if (value === original.fields[key]) continue
      fields[key] = LIST_FIELDS.some((f) => f.key === key)
        ? value.split(',').map((v) => v.trim()).filter(Boolean)
        : value
    }
    const scenes: Record<number, Record<string, unknown>> = {}
    for (const [index, value] of Object.entries(draft.scenes)) {
      if (value !== original.scenes[Number(index)]) scenes[Number(index)] = { description: value }
    }
    try {
      setMeta(await api.updateMeta(file.id, { fields, scenes }))
      setDraft(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

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
      {meta?.meta && (
        <div className="meta-edit-bar">
          {draft ? (
            <>
              <button className="primary" disabled={saving} onClick={save}>{saving ? 'Сохраняю…' : 'Сохранить'}</button>
              <button disabled={saving} onClick={() => setDraft(null)}>Отмена</button>
              <span className="hint" style={{ margin: 0 }}>Повторный анализ перепишет правки</span>
            </>
          ) : (
            <button onClick={() => setDraft(draftFrom(meta.meta!))} title="Поправить описания и теги вручную">
              Редактировать
            </button>
          )}
          {typeof meta.meta.edited_at === 'string' && !draft && (
            <span className="hint" style={{ margin: 0 }}>
              правлено {new Date(meta.meta.edited_at).toLocaleString('ru-RU')}
            </span>
          )}
        </div>
      )}
      {draft && meta?.meta && (
        <div className="meta-edit">
          {TEXT_FIELDS.filter((f) => f.key in draft.fields).map((f) => (
            <label key={f.key} className="field">
              <span>{f.label}</span>
              {f.long
                ? <textarea value={draft.fields[f.key]} onChange={(e) => setDraft({ ...draft, fields: { ...draft.fields, [f.key]: e.target.value } })} />
                : <input type="text" value={draft.fields[f.key]} onChange={(e) => setDraft({ ...draft, fields: { ...draft.fields, [f.key]: e.target.value } })} />}
            </label>
          ))}
          {LIST_FIELDS.filter((f) => f.key in draft.fields).map((f) => (
            <label key={f.key} className="field">
              <span>{f.label}</span>
              <input type="text" value={draft.fields[f.key]} onChange={(e) => setDraft({ ...draft, fields: { ...draft.fields, [f.key]: e.target.value } })} />
            </label>
          ))}
          {Object.keys(draft.scenes).length > 0 && (
            <div className="field"><span>Сцены</span></div>
          )}
          {Object.entries(draft.scenes).map(([index, value]) => {
            const scene = (meta.meta!.scenes as Array<Record<string, unknown>>)[Number(index)]
            return (
              <label key={index} className="field field-scene">
                <span>{formatDuration(Number(scene?.time ?? 0))}</span>
                <input type="text" value={value} onChange={(e) => setDraft({ ...draft, scenes: { ...draft.scenes, [Number(index)]: e.target.value } })} />
              </label>
            )
          })}
        </div>
      )}
      {!error && !draft && meta && (meta.meta
        ? <pre className="meta-json">{JSON.stringify(collapseLongArrays(meta.meta), null, 2)}</pre>
        : (
          <div className="empty">
            Файл ещё не проанализирован.<br />
            Нажмите «Переанализировать» или «Анализ всего».
          </div>
        )
      )}
    </>
  )
}
