import type { JSX } from 'react'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 全局通知（设计规范 §8：演示性功能提示统一使用 Toast） */
export function Toasts(): JSX.Element {
  const toasts = useApp((s) => s.toasts)
  return (
    <div className="toast-wrap" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast${t.leaving ? ' out' : ''}`}>
          <Icon id="i-check" />
          {t.text}
        </div>
      ))}
    </div>
  )
}
