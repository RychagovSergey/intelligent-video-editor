import { dismiss, useToasts } from '../notifications'

/** Всплывающие уведомления в правом нижнем углу; закрываются сами или по клику. */
export function Toasts() {
  const toasts = useToasts()
  if (toasts.length === 0) return null
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.level}`} onClick={() => dismiss(t.id)}>
          <div className="toast-title">{t.title}</div>
          {t.text && <div className="toast-text">{t.text}</div>}
          {t.action && (
            <button
              className="toast-action"
              onClick={(e) => { e.stopPropagation(); t.action!.run(); dismiss(t.id) }}
            >
              {t.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
