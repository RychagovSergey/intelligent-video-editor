import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { pickFolder } from '../desktop'
import type { EnvCheck, RootInfo, SettingField, SettingsView } from '../api/types'

interface Props {
  onClose: () => void
  /** Настройки сохранены — родитель перечитывает health/models. */
  onSaved: () => void
  /** Список папок хранилища изменился — родитель перечитывает дерево и счётчики. */
  onRootsChanged: () => void
}

const CHECK_TAB = 'check'
const ROOTS_TAB = 'roots'

/**
 * Панель настроек (ТЗ п. 3.7): правит `.env` через backend, тот перечитывает значения
 * на лету. Ключи API приходят маской и отправляются обратно только если их переписали.
 */
export function SettingsDialog({ onClose, onSaved, onRootsChanged }: Props) {
  const [view, setView] = useState<SettingsView | null>(null)
  const [checks, setChecks] = useState<EnvCheck[] | null>(null)
  const [tab, setTab] = useState<string>(CHECK_TAB)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = () => {
    api.settings().then((v) => { setView(v); setDraft({}) }).catch((e: Error) => setError(e.message))
    setChecks(null)
    api.checkEnv().then(setChecks).catch(() => setChecks([]))
  }
  useEffect(load, [])

  useEffect(() => {
    // Если окружение в порядке, открываемся сразу на первой группе, а не на пустой проверке.
    if (checks && view && tab === CHECK_TAB && checks.every((c) => c.level === 'ok')) {
      setTab(view.groups[0].id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checks, view])

  const valueOf = (f: SettingField) => (f.key in draft ? draft[f.key] : String(f.value ?? ''))
  const dirty = Object.keys(draft).length > 0

  const save = async () => {
    if (!view) return
    setBusy(true)
    setError(null)
    try {
      const saved = await api.saveSettings(draft)
      setView(saved)
      setDraft({})
      if (saved.restart_required) {
        setNotice('Сохранено. Папка кеша применится после перезапуска backend.')
      } else if (saved.saved && saved.saved.length > 0) {
        setNotice(`Сохранено: ${saved.saved.join(', ')}`)
      } else {
        setNotice('Изменений нет')
      }
      api.checkEnv().then(setChecks).catch(() => undefined)
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const problems = checks?.filter((c) => c.level !== 'ok').length ?? 0

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-settings" onClick={(e) => e.stopPropagation()}>
        <h2>Настройки</h2>
        <div className="tabs">
          <button className={tab === CHECK_TAB ? 'active' : ''} onClick={() => setTab(CHECK_TAB)}>
            Проверка{problems > 0 && <span className="tab-badge">{problems}</span>}
          </button>
          <button className={tab === ROOTS_TAB ? 'active' : ''} onClick={() => setTab(ROOTS_TAB)}>
            Хранилище
          </button>
          {view?.groups.map((g) => (
            <button key={g.id} className={tab === g.id ? 'active' : ''} onClick={() => setTab(g.id)}>
              {g.title}
            </button>
          ))}
        </div>

        <div className="settings-body">
          {tab === ROOTS_TAB && <RootsEditor onChanged={onRootsChanged} />}
          {tab === CHECK_TAB && (
            <EnvChecks checks={checks} onRefresh={() => { setChecks(null); api.checkEnv().then(setChecks).catch(() => setChecks([])) }} />
          )}
          {view?.groups.filter((g) => g.id === tab).map((g) => (
            <div key={g.id}>
              {g.fields.map((f) => (
                <Field
                  key={f.key}
                  field={f}
                  value={valueOf(f)}
                  changed={f.key in draft}
                  onChange={(v) => setDraft((d) => {
                    const next = { ...d }
                    if (v === String(f.value ?? '')) delete next[f.key]
                    else next[f.key] = v
                    return next
                  })}
                />
              ))}
            </div>
          ))}
        </div>

        {view && (
          <div className="hint" style={{ marginTop: 10 }}>
            Файл: {view.env_file}{!view.env_exists && ' (будет создан из .env.example)'}
          </div>
        )}
        {error && <div className="error-text" style={{ marginTop: 6 }}>{error}</div>}
        {notice && !error && <div className="hint" style={{ color: 'var(--ok)' }}>{notice}</div>}

        <div className="modal-actions">
          <button onClick={onClose}>{dirty ? 'Отмена' : 'Закрыть'}</button>
          <button className="primary" disabled={!dirty || busy} onClick={save}>
            {busy ? 'Сохраняю…' : 'Сохранить'}
          </button>
        </div>
      </div>
    </div>
  )
}


function Field({ field, value, changed, onChange }: {
  field: SettingField
  value: string
  changed: boolean
  onChange: (v: string) => void
}) {
  const [reveal, setReveal] = useState(false)
  const common = {
    value,
    disabled: field.overridden,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => onChange(e.target.value),
  }

  let control: React.ReactNode
  if (field.kind === 'select') {
    const options = field.options.includes(value) ? field.options : [value, ...field.options]
    control = (
      <select {...common}>
        {options.map((o) => <option key={o} value={o}>{o === '' ? '— не задано —' : o}</option>)}
      </select>
    )
  } else if (field.kind === 'secret') {
    control = (
      <div className="field-inline">
        <input
          type={reveal ? 'text' : 'password'}
          autoComplete="off"
          placeholder="не задан"
          {...common}
          onFocus={(e) => { if (!changed) e.target.select() }}
        />
        {changed && (
          <button type="button" onClick={() => setReveal(!reveal)} title="Показать введённое">
            {reveal ? '🙈' : '👁'}
          </button>
        )}
      </div>
    )
  } else {
    control = (
      <input
        type="text"
        inputMode={field.kind === 'text' ? undefined : 'decimal'}
        placeholder={field.placeholder || (field.default !== '' ? `по умолчанию ${field.default}` : '')}
        spellCheck={false}
        {...common}
      />
    )
  }

  return (
    <label className={`field${changed ? ' field-changed' : ''}`}>
      <span>
        {field.label}
        {field.restart && <em className="field-flag" title="Применится после перезапуска backend">перезапуск</em>}
      </span>
      {control}
      {field.overridden && (
        <div className="field-hint error-text">
          Задано переменной окружения процесса {field.key} — она сильнее .env, правка здесь не подействует.
        </div>
      )}
      {field.hint && !field.overridden && <div className="field-hint">{field.hint}</div>}
    </label>
  )
}


const ICON = { ok: '✓', warn: '!', error: '✕' } as const

function EnvChecks({ checks, onRefresh }: { checks: EnvCheck[] | null; onRefresh: () => void }) {
  if (checks === null) return <div className="empty">Проверяю окружение…</div>
  return (
    <div className="env-checks">
      {checks.map((c) => (
        <div key={c.id} className={`env-check env-${c.level}`}>
          <span className="env-icon">{ICON[c.level]}</span>
          <div>
            <div>{c.text}</div>
            {c.hint && <div className="field-hint">{c.hint}</div>}
          </div>
        </div>
      ))}
      <button style={{ alignSelf: 'flex-start', marginTop: 6 }} onClick={onRefresh}>Проверить снова</button>
    </div>
  )
}


/** Папки хранилища (ТЗ п. 3.1, Р-11): живут в базе, .env задаёт лишь стартовое значение. */
function RootsEditor({ onChanged }: { onChanged: () => void }) {
  const [roots, setRoots] = useState<RootInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.roots().then((r) => setRoots(r.roots)).catch((e: Error) => setError(e.message))
  }, [])

  const apply = async (next: string[]) => {
    setBusy(true)
    setError(null)
    try {
      setRoots((await api.setRoots(next)).roots)
      onChanged()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const add = async () => {
    const picked = await pickFolder('Папка с медиатекой')
    if (!picked || !picked.trim()) return
    const current = roots?.map((r) => r.path) ?? []
    if (current.includes(picked)) return
    await apply([...current, picked.trim()])
  }

  if (roots === null && !error) return <div className="empty">Загружаю…</div>

  return (
    <div className="roots-editor">
      {roots?.length === 0 && (
        <div className="hint">Папок пока нет — добавьте, откуда брать материал, и нажмите «Сканировать».</div>
      )}
      {roots?.map((r) => (
        <div key={r.path} className={`root-row${r.exists && r.is_dir ? '' : ' root-missing'}`}>
          <span className="root-path" title={r.path}>{r.path}</span>
          {!(r.exists && r.is_dir) && <span className="error-text">не найдена</span>}
          <button
            disabled={busy}
            onClick={() => apply(roots.filter((x) => x.path !== r.path).map((x) => x.path))}
            title="Убрать папку из хранилища; файлы на диске не трогаются, индекс по ней очищается"
          >
            Убрать
          </button>
        </div>
      ))}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
        <button disabled={busy} onClick={add}>Добавить папку…</button>
        <span className="field-hint">Кеш (индекс, метаданные, прокси) лежит в `data` внутри первой папки, если не задан DATA_DIR.</span>
      </div>
      {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
    </div>
  )
}
