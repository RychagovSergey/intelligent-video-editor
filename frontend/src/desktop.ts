/**
 * Мост к оболочке Tauri (этап 8). В браузере всё это выключено: тот же интерфейс
 * работает через Vite и системные диалоги заменяются на текстовый ввод пути.
 */

declare global {
  interface Window {
    /** Адрес backend, который оболочка подставляет до загрузки скриптов (см. src-tauri/src/lib.rs). */
    __IVE_BACKEND__?: string
    __TAURI_INTERNALS__?: unknown
  }
}

export const isDesktop = (): boolean => typeof window !== 'undefined' && window.__TAURI_INTERNALS__ != null

/** Нативный выбор папки; в браузере — обычный prompt с тем же контрактом (null — отмена). */
export async function pickFolder(title: string, defaultPath?: string): Promise<string | null> {
  if (isDesktop()) {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const picked = await open({ directory: true, multiple: false, title, defaultPath })
    return typeof picked === 'string' ? picked : null
  }
  return window.prompt(title, defaultPath ?? '')
}
