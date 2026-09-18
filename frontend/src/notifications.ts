/**
 * Уведомления о завершении фоновых задач (ТЗ п. 3.7).
 *
 * Крошечное хранилище без контекста React: сообщить о событии может любой модуль,
 * а показывает их один компонент `Toasts`. Когда окно не на переднем плане, событие
 * дублируется системным уведомлением — анализ библиотеки идёт часами, и человек за
 * это время уходит в другие приложения.
 */
import { useEffect, useState } from 'react'

export type ToastLevel = 'info' | 'ok' | 'warn' | 'error'

export interface Toast {
  id: number
  level: ToastLevel
  title: string
  text?: string
  /** Кнопка действия — например, «Показать в Finder» для готового экспорта. */
  action?: { label: string; run: () => void }
  sticky?: boolean
}

type Listener = (toasts: Toast[]) => void

let toasts: Toast[] = []
let nextId = 1
const listeners = new Set<Listener>()
const AUTO_HIDE_MS = 7000

function emit() {
  for (const fn of listeners) fn(toasts)
}

export function dismiss(id: number) {
  toasts = toasts.filter((t) => t.id !== id)
  emit()
}

export function notify(toast: Omit<Toast, 'id'>): number {
  const id = nextId++
  toasts = [...toasts, { ...toast, id }]
  emit()
  if (!toast.sticky) window.setTimeout(() => dismiss(id), AUTO_HIDE_MS)
  if (document.hidden) void systemNotification(toast.title, toast.text)
  return id
}

/** Системное уведомление — только если пользователь его уже разрешил или разрешит сейчас. */
async function systemNotification(title: string, body?: string) {
  if (typeof Notification === 'undefined') return
  try {
    if (Notification.permission === 'default') await Notification.requestPermission()
    if (Notification.permission === 'granted') new Notification(title, { body })
  } catch {
    // Браузер без Notification API или запрет в настройках — тост всё равно показан.
  }
}

/** Разрешение спрашиваем заранее, при первом запуске задачи, а не в момент её завершения. */
export function requestNotificationPermission() {
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    void Notification.requestPermission().catch(() => undefined)
  }
}

export function useToasts(): Toast[] {
  const [state, setState] = useState<Toast[]>(toasts)
  useEffect(() => {
    listeners.add(setState)
    return () => { listeners.delete(setState) }
  }, [])
  return state
}
