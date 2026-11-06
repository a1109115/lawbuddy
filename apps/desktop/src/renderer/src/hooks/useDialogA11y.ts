import { useEffect, useRef, type RefObject } from 'react'

/**
 * Esc 关闭浮层（设计规范 §7：Esc 层级：关闭浮层 → 关闭产物弹窗 → 退出全屏）。
 * active=false 时不挂监听；回调经 ref 转发，避免监听器随渲染重挂。
 */
export function useEscapeKey(onClose: () => void, active = true): void {
  const cb = useRef(onClose)
  cb.current = onClose
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') cb.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [active])
}

/**
 * 模态弹窗无障碍（DEV-UI-008）：激活时聚焦卡片（读屏立即播报 aria-label），
 * Esc 关闭。卡片需设 ref 与 tabIndex={-1}。active 控制开合（组件常驻挂载时传入 open 状态）。
 */
export function useDialogA11y(onClose: () => void, active = true): RefObject<HTMLDivElement | null> {
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (!active) return
    ref.current?.focus()
  }, [active])
  useEscapeKey(onClose, active)
  return ref
}
