import { useLayoutEffect, useRef, useState, type JSX } from 'react'
import type { CiteMarkInfo } from '../state/store'

/** 弹窗与容器边缘的最小间距 / 弹窗半宽（CSS 中 .cite-pop 宽 260px） */
const EDGE_MARGIN = 8
const POP_HALF_WIDTH = 130

/**
 * 向上查找最近的裁剪祖先（overflow 非 visible 的可滚动容器，如聊天消息流）。
 * 弹窗是绝对定位元素，超出该容器边界会被裁剪，定位需以它为界钳制。
 */
function findClipAncestor(el: HTMLElement | null): HTMLElement | null {
  let p = el?.parentElement ?? null
  while (p) {
    const s = getComputedStyle(p)
    if (s.overflowX !== 'visible' || s.overflowY !== 'visible') return p
    p = p.parentElement
  }
  return null
}

/**
 * 回答气泡内的原文引用角标（样式参照设计稿：上标小数字）。
 *
 * - 悬浮 150ms 后弹出出处卡：文档名（可点，跳转阅读视图对应页）、页码、原文片段；
 * - 已校验（蓝）/ 待核实（黄，点击打开核实队列）/ 未解析（灰，文档不在案件内）；
 * - 无校验数据（旧任务）时降级为仅悬浮显示标题与页码；
 * - 出处卡自适应定位：靠边时水平钳制在滚动容器内，上方空间不足时翻转到下方。
 */
export function CiteMark({
  seq,
  info,
  onCite
}: {
  seq: number
  info: CiteMarkInfo
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const [hover, setHover] = useState(false)
  /** 弹窗水平偏移（相对默认居中位）与是否翻转到下方 */
  const [pos, setPos] = useState({ dx: 0, below: false })
  const wrapRef = useRef<HTMLSpanElement>(null)
  const popRef = useRef<HTMLSpanElement>(null)

  // hover 渲染后、绘制前实测：钳制水平位置 + 判断上下翻转
  useLayoutEffect(() => {
    if (!hover) return
    const wrap = wrapRef.current
    const pop = popRef.current
    if (!wrap || !pop) return
    const bounds = (findClipAncestor(wrap) ?? document.documentElement).getBoundingClientRect()
    const wr = wrap.getBoundingClientRect()
    const center = wr.left + wr.width / 2
    const min = bounds.left + EDGE_MARGIN + POP_HALF_WIDTH
    const max = bounds.right - EDGE_MARGIN - POP_HALF_WIDTH
    const dx = Math.min(Math.max(center, min), max) - center
    const ph = pop.offsetHeight
    const roomAbove = wr.top - bounds.top
    const roomBelow = bounds.bottom - wr.bottom
    const below = roomAbove < ph + EDGE_MARGIN * 2 && roomBelow > roomAbove
    setPos({ dx, below })
  }, [hover])

  const pending = info.claimStatus === 'needs_human' || info.claimStatus === 'conflict'
  const unresolved = !info.documentId
  const stateClass = unresolved ? ' unresolved' : pending ? ' pending' : ''
  /** 页码文案：第N页 / 第N-M页（区间引用）；pageNo=0 为整文档级引用（无页码） */
  const pageLabel = info.pageEnd ? `${info.pageNo}-${info.pageEnd}` : info.pageNo > 0 ? String(info.pageNo) : ''
  const jumpPage = info.pageNo > 0 ? info.pageNo : 1
  return (
    <span
      ref={wrapRef}
      className={`cite-mark-wrap${pos.below ? ' below' : ''}`}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        type="button"
        className={`cite-mark${stateClass}`}
        aria-label={`引用来源：《${info.title}》${pageLabel ? `第${pageLabel}页` : ''}`}
        onClick={() => {
          if (info.documentId) onCite(info.documentId, jumpPage)
        }}
      >
        {seq}
      </button>
      {hover && (
        <span
          ref={popRef}
          className={`cite-pop${pos.below ? ' below' : ''}`}
          role="tooltip"
          style={pos.dx ? { left: `calc(50% + ${pos.dx}px)` } : undefined}
        >
          <span
            className={`cite-pop-title${info.documentId ? ' link' : ''}`}
            onClick={() => {
              if (info.documentId) onCite(info.documentId, jumpPage)
            }}
          >
            {info.title}
          </span>
          {pageLabel && <span className="cite-pop-page">第{pageLabel}页</span>}
          {info.snippet && <span className="cite-pop-snippet">…{info.snippet}…</span>}
          {pending && <span className="cite-pop-warn">待人工核实：引语与原文匹配未通过</span>}
          {unresolved && <span className="cite-pop-warn">未在案件文档中找到该文档</span>}
        </span>
      )}
    </span>
  )
}
