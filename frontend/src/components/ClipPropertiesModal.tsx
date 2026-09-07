import { useState } from 'react'
import { api } from '../api/client'
import type { Clip, TextAnimation, TextPosition, TimelineState, TransitionKind } from '../api/types'

interface Props {
  projectId: number
  clip: Clip
  onClose: () => void
  onChange: (next: TimelineState) => void
}

const TRANSITION_KINDS: { value: TransitionKind; label: string }[] = [
  { value: 'crossfade', label: 'Кроссфейд' },
  { value: 'dip_to_black', label: 'Dip to black' },
]
const POSITIONS: { value: TextPosition; label: string }[] = [
  { value: 'top', label: 'Сверху' },
  { value: 'center', label: 'По центру' },
  { value: 'bottom', label: 'Снизу' },
]

/** Двойной клик по клипу — точные параметры вплоть до миллисекунд, а не только пресеты тулбара. */
export function ClipPropertiesModal({ projectId, clip, onClose, onChange }: Props) {
  const isText = clip.kind === 'text'
  const isVideoTrack = clip.kind === 'video' || clip.kind === 'image'

  const [inPoint, setInPoint] = useState(clip.in_point)
  const [outPoint, setOutPoint] = useState(clip.out_point)
  const [start, setStart] = useState(clip.start)
  const [speed, setSpeed] = useState(clip.speed)
  const [fadeIn, setFadeIn] = useState(clip.fade_in)
  const [fadeOut, setFadeOut] = useState(clip.fade_out)
  const [muted, setMuted] = useState(clip.muted)
  const [transitionOn, setTransitionOn] = useState(!!clip.transition_in)
  const [transitionKind, setTransitionKind] = useState<TransitionKind>(clip.transition_in?.kind ?? 'crossfade')
  const [transitionDuration, setTransitionDuration] = useState(clip.transition_in?.duration ?? 0.5)
  const [text, setText] = useState(clip.text ?? '')
  const [fontSize, setFontSize] = useState(clip.font_size ?? 48)
  const [position, setPosition] = useState<TextPosition>(clip.position ?? 'bottom')
  const [animation, setAnimation] = useState<TextAnimation>(clip.animation ?? 'fade')

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      let state: TimelineState | null = null

      if (isText) {
        if (outPoint !== clip.out_point) {
          state = await api.trimClip(projectId, clip.id, { in_point: 0, out_point: outPoint })
        }
        if (
          text !== clip.text || fontSize !== clip.font_size ||
          position !== clip.position || animation !== clip.animation
        ) {
          state = await api.setTextProperties(projectId, clip.id, { text, font_size: fontSize, position, animation })
        }
      } else {
        if (inPoint !== clip.in_point || outPoint !== clip.out_point) {
          state = await api.trimClip(projectId, clip.id, { in_point: inPoint, out_point: outPoint })
        }
        if (speed !== clip.speed) {
          state = await api.setClipSpeed(projectId, clip.id, speed)
        }
        if (fadeIn !== clip.fade_in || fadeOut !== clip.fade_out) {
          state = await api.setClipFade(projectId, clip.id, { fade_in: fadeIn, fade_out: fadeOut })
        }
        if (muted !== clip.muted) {
          state = await api.muteClip(projectId, clip.id, muted)
        }
      }

      if (start !== clip.start) {
        state = await api.moveClip(projectId, clip.id, start)
      }

      if (isVideoTrack) {
        const had = clip.transition_in
        const changed = transitionOn
          ? (!had || had.kind !== transitionKind || had.duration !== transitionDuration)
          : !!had
        if (changed) {
          state = transitionOn
            ? await api.setClipTransition(projectId, clip.id, transitionKind, transitionDuration)
            : await api.clearClipTransition(projectId, clip.id)
        }
      }

      if (state) onChange(state)
      onClose()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <h2>{isText ? 'Текстовый слой' : `Клип: ${clip.name}`}</h2>

        {isText && (
          <label className="field">
            <span>Текст</span>
            <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={200} />
          </label>
        )}

        <div className="field-row">
          <label className="field">
            <span>Начало на таймлайне, с</span>
            <input type="number" step="0.001" value={start} onChange={(e) => setStart(Number(e.target.value))} />
          </label>
          {isText ? (
            <label className="field">
              <span>Длительность, с</span>
              <input
                type="number" step="0.001" min="0.001" value={outPoint}
                onChange={(e) => setOutPoint(Number(e.target.value))}
              />
            </label>
          ) : (
            <label className="field">
              <span>Скорость</span>
              <input
                type="number" step="0.01" min="0.25" max="4" value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
              />
            </label>
          )}
        </div>

        {!isText && (
          <div className="field-row">
            <label className="field">
              <span>Вход в исходнике, с</span>
              <input
                type="number" step="0.001" min="0" value={inPoint}
                onChange={(e) => setInPoint(Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Выход в исходнике, с</span>
              <input
                type="number" step="0.001" min="0" value={outPoint}
                onChange={(e) => setOutPoint(Number(e.target.value))}
              />
            </label>
          </div>
        )}

        {!isText && (
          <div className="field-row">
            <label className="field">
              <span>Fade in, с</span>
              <input
                type="number" step="0.001" min="0" value={fadeIn}
                onChange={(e) => setFadeIn(Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Fade out, с</span>
              <input
                type="number" step="0.001" min="0" value={fadeOut}
                onChange={(e) => setFadeOut(Number(e.target.value))}
              />
            </label>
          </div>
        )}

        {!isText && (
          <label className="field field-checkbox">
            <input type="checkbox" checked={muted} onChange={(e) => setMuted(e.target.checked)} />
            <span>выключить собственный звук клипа</span>
          </label>
        )}

        {isText && (
          <div className="field-row">
            <label className="field">
              <span>Размер шрифта</span>
              <input
                type="number" step="1" min="8" max="300" value={fontSize}
                onChange={(e) => setFontSize(Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span>Позиция</span>
              <select value={position} onChange={(e) => setPosition(e.target.value as TextPosition)}>
                {POSITIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </label>
          </div>
        )}

        {isText && (
          <label className="field">
            <span>Анимация</span>
            <select value={animation} onChange={(e) => setAnimation(e.target.value as TextAnimation)}>
              <option value="fade">Fade in/out</option>
              <option value="none">Без анимации</option>
            </select>
          </label>
        )}

        {isVideoTrack && (
          <>
            <label className="field field-checkbox">
              <input type="checkbox" checked={transitionOn} onChange={(e) => setTransitionOn(e.target.checked)} />
              <span>Переход от предыдущего клипа</span>
            </label>
            {transitionOn && (
              <div className="field-row">
                <label className="field">
                  <span>Тип</span>
                  <select value={transitionKind} onChange={(e) => setTransitionKind(e.target.value as TransitionKind)}>
                    {TRANSITION_KINDS.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </label>
                <label className="field">
                  <span>Длительность, с</span>
                  <input
                    type="number" step="0.01" min="0.01" value={transitionDuration}
                    onChange={(e) => setTransitionDuration(Number(e.target.value))}
                  />
                </label>
              </div>
            )}
          </>
        )}

        {error && <div className="error-text">{error}</div>}

        <div className="modal-actions">
          <button onClick={onClose}>Отмена</button>
          <button className="primary" onClick={save} disabled={busy}>
            {busy ? 'Сохраняю…' : 'Сохранить'}
          </button>
        </div>
      </div>
    </div>
  )
}
