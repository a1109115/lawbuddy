import { useEffect, useMemo, useRef, useState, type JSX, type MouseEvent } from 'react'
import { Icon } from './Icon'

/**
 * mermaid 图渲染（关系图/流程图共用布局引擎，PRD 13.4 / ADR-005）：
 * - DSL 由结构化数据在客户端确定性生成：模型文本只进入引号内的 label，
 *   节点 id 重映射为 m0..mN，模型 id 不进入 DSL，不存在模型产出可解析脚本/HTML 的通路；
 * - mermaid 以 securityLevel:'strict' + htmlLabels:false 运行（v11 内置 DOMPurify 消毒输出），
 *   纯 SVG 渲染、无外部资源请求（本地优先，CSP 兜底）；
 * - needs_human 节点带 △ 文字标记 + mgWarn 琥珀虚线描边（不只靠颜色）；
 * - 点击节点回调节点业务 id，引证展示与原文跳转由父组件的详情面板承接；
 * - 渲染失败经 onRenderError 通知父组件回退（径向 SVG / 表格降级，REQ-VIS-004）。
 */

/** mermaid 图节点（relationGraph 与 FlowSpec 共用的最小结构） */
export interface MgNode {
  id: string
  label: string
  shape?: string
  kind?: string
  status?: string
}

export interface MgEdge {
  from: string
  to: string
  label?: string
  dashed?: boolean
  status?: string
}

const FLOW_SHAPES = new Set(['rect', 'stadium', 'round', 'diamond', 'circle'])
const LABEL_MAX = 40

/** label 转义：只进引号内文本；替换引号/竖线/花括号/尖括号与换行，避免破坏 DSL 结构 */
function escLabel(s: string): string {
  return s
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, LABEL_MAX)
    .replace(/"/g, '#quot;')
    .replace(/[|{}<>]/g, (c) => ({ '|': '｜', '{': '（', '}': '）', '<': '＜', '>': '＞' })[c] ?? c)
}

function nodeToken(id: string, n: MgNode): string {
  const label = escLabel(n.label)
  switch (n.shape) {
    case 'stadium':
      return `${id}(["${label}"])`
    case 'round':
      return `${id}("${label}")`
    case 'diamond':
      return `${id}{"${label}"}`
    case 'circle':
      return `${id}(("${label}"))`
    default:
      return `${id}["${label}"]`
  }
}

/** 从结构化数据确定性生成 mermaid flowchart DSL（节点 id m0..mN 按入参顺序） */
export function buildFlowDsl(nodes: MgNode[], edges: MgEdge[], direction: string): string {
  const idOf = new Map(nodes.map((n, i) => [n.id, `m${i}`]))
  const lines = [`flowchart ${direction === 'LR' ? 'LR' : 'TB'}`]
  const warnIds: string[] = []
  const orgIds: string[] = []
  for (const n of nodes) {
    const mid = idOf.get(n.id)
    if (!mid) continue
    if (n.status === 'needs_human') {
      warnIds.push(mid)
      lines.push(`  ${nodeToken(mid, { ...n, label: `△ ${n.label}` })}`)
    } else {
      lines.push(`  ${nodeToken(mid, n)}`)
    }
    if (n.kind === 'org') orgIds.push(mid)
  }
  for (const e of edges) {
    const a = idOf.get(e.from)
    const b = idOf.get(e.to)
    if (!a || !b) continue
    const label = e.label ? `|${escLabel(e.label)}|` : ''
    lines.push(`  ${a} ${e.dashed ? '-.->' : '-->'}${label} ${b}`)
  }
  lines.push('  classDef mgWarn stroke-width:2.4px')
  lines.push('  classDef mgOrg stroke-width:2px')
  if (warnIds.length) lines.push(`  class ${warnIds.join(',')} mgWarn`)
  if (orgIds.length) lines.push(`  class ${orgIds.join(',')} mgOrg`)
  return lines.join('\n')
}

let renderSeq = 0

export function MermaidGraph({
  nodes,
  edges,
  direction = 'TB',
  selectedId,
  onSelectNode,
  onRenderError,
  ariaLabel
}: {
  nodes: MgNode[]
  edges: MgEdge[]
  direction?: string
  selectedId?: string | null
  onSelectNode?: (id: string | null) => void
  onRenderError?: () => void
  ariaLabel?: string
}): JSX.Element {
  const [phase, setPhase] = useState<'loading' | 'ok' | 'error'>('loading')
  const [zoom, setZoom] = useState(1)
  const boxRef = useRef<HTMLDivElement | null>(null)

  const dsl = useMemo(() => buildFlowDsl(nodes, edges, direction), [nodes, edges, direction])

  // 渲染：mermaid 惰性加载（与 markdown-it 同模式），strict 模式消毒，失败上报父组件降级
  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    void (async () => {
      try {
        const mermaid = (await import('mermaid')).default
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: 'base',
          fontFamily: "-apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif",
          themeVariables: {
            primaryColor: '#f0f5ff',
            primaryTextColor: '#16233f',
            primaryBorderColor: '#2f6bff',
            lineColor: '#9fb6dd',
            edgeLabelBackground: '#ffffff',
            fontSize: '12px'
          },
          flowchart: { htmlLabels: false, curve: 'basis', useMaxWidth: false }
        })
        const { svg } = await mermaid.render(`mg-render-${++renderSeq}`, dsl)
        if (cancelled || !boxRef.current) return
        // 结构化注入：DOMParser 惰性解析（不执行脚本/不加载资源），仅导入
        // 清理后的 svg 节点（剥离 script/foreignObject 与 on* 事件属性），
        // 渲染字符串不经过任何原始 HTML 字符串入口
        const parsed = new DOMParser().parseFromString(svg, 'image/svg+xml')
        const svgRoot = parsed.documentElement
        if (!svgRoot || svgRoot.nodeName !== 'svg' || parsed.querySelector('parsererror')) {
          throw new Error('mermaid svg parse failed')
        }
        svgRoot.querySelectorAll('script,foreignObject').forEach((el) => el.remove())
        svgRoot.querySelectorAll('*').forEach((el) => {
          for (const attr of Array.from(el.attributes)) {
            if (/^on/i.test(attr.name)) el.removeAttribute(attr.name)
          }
        })
        boxRef.current.replaceChildren(document.importNode(svgRoot, true))
        setPhase('ok')
      } catch {
        if (!cancelled) {
          setPhase('error')
          onRenderError?.()
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [dsl])

  // 缩放：以 viewBox 真实尺寸为基准设置像素宽，容器滚动承接溢出
  useEffect(() => {
    const svg = boxRef.current?.querySelector('svg')
    if (!svg) return
    const vb = svg.viewBox.baseVal
    if (vb.width > 0) {
      svg.style.maxWidth = 'none'
      svg.style.width = `${Math.round(vb.width * zoom)}px`
      svg.style.height = 'auto'
    }
  }, [zoom, phase])

  // 选中高亮：直接切 DOM class，不重渲染布局
  useEffect(() => {
    const box = boxRef.current
    if (!box || phase !== 'ok') return
    box.querySelectorAll('g.node').forEach((g) => {
      const m = /flowchart-m(\d+)/.exec(g.id)
      const idx = m ? Number(m[1]) : -1
      g.classList.toggle('mg-sel', idx >= 0 && nodes[idx]?.id === selectedId)
    })
  }, [phase, selectedId, nodes])

  // 事件委托：g.node id 形如 flowchart-m0-1，提取序号映射回业务节点
  const onGraphClick = (e: MouseEvent<HTMLDivElement>): void => {
    const g = (e.target as Element).closest('g.node')
    if (!g) return
    const m = /flowchart-m(\d+)/.exec(g.id)
    if (!m) return
    const node = nodes[Number(m[1])]
    if (!node) return
    onSelectNode?.(selectedId === node.id ? null : node.id)
  }

  const warnCount = nodes.filter((n) => n.status === 'needs_human').length

  return (
    <div className="mg-wrap">
      <div className="rg-tools">
        <button className="rg-tool" aria-label="放大" title="放大" onClick={() => setZoom(Math.min(2, zoom + 0.15))}>
          <Icon id="i-plus" />
        </button>
        <span className="rg-zoom mono">{Math.round(zoom * 100)}%</span>
        <button className="rg-tool" aria-label="缩小" title="缩小" onClick={() => setZoom(Math.max(0.5, zoom - 0.15))}>
          <Icon id="i-minus" />
        </button>
        <span className="rg-hint">
          点击节点查看事实与来源{warnCount > 0 ? ` · △ 待人工核实（${warnCount}）` : ''}
        </span>
      </div>
      <div className="rg-scroll mg-scroll">
        {phase === 'loading' && <div className="mg-loading">图表渲染中…</div>}
        {phase === 'error' && <div className="viz-empty">图形渲染失败，已切换降级视图。</div>}
        <div
          ref={boxRef}
          className="mg-box mg-svg"
          onClick={onGraphClick}
          aria-label={ariaLabel}
          hidden={phase !== 'ok'}
        />
      </div>
    </div>
  )
}
