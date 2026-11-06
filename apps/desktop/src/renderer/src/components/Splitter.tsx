import { useCallback, useEffect, useRef, useState, type JSX, type PointerEvent as RPointerEvent } from 'react'
import { DEF_LEFT, DEF_RIGHT, LEFT_MAX, LEFT_MIN, RIGHT_MIN, useApp } from '../state/store'
import { Icon } from './Icon'

/**
 * 面板分隔条（PRD 9.2 / 设计规范 §5.1）：
 * - 拖拽实时调宽（左 180–340，右 280–窗口一半）；
 * - 原地点击折叠，折叠后点击恢复记忆宽度；
 * - 双击恢复默认宽度；
 * - 键盘：Enter/空格折叠，方向键 ±16px 微调。
 */
export function Splitter({ side }: { side: 'left' | 'right' }): JSX.Element {
  const { leftW, rightW, leftHide, rightHide, setLeftW, setRightW, toggleLeft, toggleRight, resetLeft, resetRight } =
    useApp()
  const [dragging, setDragging] = useState(false)
  const startX = useRef(0)
  const startW = useRef(0)
  const moved = useRef(false)
  const ref = useRef<HTMLDivElement>(null)

  const isLeft = side === 'left'
  const collapsed = isLeft ? leftHide : rightHide
  const width = isLeft ? leftW : rightW
  const setW = isLeft ? setLeftW : setRightW
  const toggle = isLeft ? toggleLeft : toggleRight
  const reset = isLeft ? resetLeft : resetRight
  const min = isLeft ? LEFT_MIN : RIGHT_MIN
  const max = isLeft ? LEFT_MAX : Math.floor(window.innerWidth / 2)

  const setPanelAnim = (on: boolean): void => {
    const panel = ref.current?.previousElementSibling ?? ref.current?.nextElementSibling
    panel?.classList.toggle('no-anim', !on)
  }

  const onPointerDown = (e: RPointerEvent<HTMLDivElement>): void => {
    if (collapsed) {
      toggle()
      return
    }
    startX.current = e.clientX
    startW.current = width
    moved.current = false
    e.currentTarget.setPointerCapture(e.pointerId)
    setDragging(true)
    setPanelAnim(false)
    document.body.classList.add('col-resizing')
  }

  const onPointerMove = (e: RPointerEvent<HTMLDivElement>): void => {
    if (!dragging) return
    const dx = e.clientX - startX.current
    if (Math.abs(dx) > 3) moved.current = true
    const next = isLeft ? startW.current + dx : startW.current - dx
    setW(Math.max(min, Math.min(max, next)))
  }

  const endDrag = useCallback((): void => {
    if (!dragging) return
    setDragging(false)
    setPanelAnim(true)
    document.body.classList.remove('col-resizing')
    if (!moved.current) toggle() // 原地点击：折叠
  }, [dragging, toggle])

  useEffect(() => {
    if (!dragging) return
    const up = (): void => endDrag()
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
    return () => {
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
    }
  }, [dragging, endDrag])

  const onKeyDown = (e: React.KeyboardEvent): void => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      toggle()
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      if (collapsed) return
      const d = (e.key === 'ArrowLeft' ? -16 : 16) * (isLeft ? 1 : -1)
      setW(Math.max(min, Math.min(max, width + d)))
      e.preventDefault()
    }
  }

  const chevron = isLeft ? (collapsed ? 'i-chevr' : 'i-chevl') : collapsed ? 'i-chevl' : 'i-chevr'

  return (
    <div
      ref={ref}
      className={`splitter${dragging ? ' dragging' : ''}`}
      role="separator"
      aria-orientation="vertical"
      aria-expanded={!collapsed}
      tabIndex={0}
      title={`拖动调节宽度 · 点击折叠${isLeft ? '左侧栏' : '右侧面板'} · 双击恢复默认`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onDoubleClick={reset}
      onKeyDown={onKeyDown}
    >
      <div className="sp-handle">
        <Icon id={chevron} />
      </div>
    </div>
  )
}

export { DEF_LEFT, DEF_RIGHT }
