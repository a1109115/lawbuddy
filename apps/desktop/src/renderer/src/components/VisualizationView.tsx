import { useEffect, useMemo, useState, type JSX } from 'react'
import type { FlowSpec, RelationEdge, RelationNode, TimelineNode, VizSpec } from '@lawbuddy/contracts'
import { CiteBadge } from './CiteBadge'
import { Icon } from './Icon'
import { MermaidGraph, type MgEdge, type MgNode } from './MermaidGraph'

/**
 * 可视化阅卷渲染器（PRD 13.4 / REQ-VIS）：
 * - 时间线：日期轨道 + 事件卡，节点引证可跳转原文（REQ-VIS-002）；
 * - 人物关系：结构化数据经客户端确定性生成 mermaid DSL 渲染（ADR-005：不执行
 *   模型生成的脚本）；渲染失败自动回退本地 SVG 径向布局；
 * - 流程/证据关系图：FlowSpec 经 mermaid 渲染（流程类/关系类表达），失败回退表格；
 * - 表格降级（REQ-VIS-004）：任一图表可切换为结构化表格阅读；
 * - 每个事实节点带引证或待核实标记（REQ-VIS-001，needs_human 节点 △ 文字 + 琥珀描边，
 *   不只靠颜色）。
 */

type Selection = { kind: 'node'; node: RelationNode } | { kind: 'edge'; edge: RelationEdge } | null

const STATUS_TEXT: Record<string, string> = {
  supported: '已支持',
  needs_human: '待人工核实',
  pending: '待核实',
  conflict: '来源冲突',
  stale: '版本失效'
}

/** 状态文字（与图标/描边共同表达，不只靠颜色） */
function statusText(s?: string): string {
  return s ? (STATUS_TEXT[s] ?? '') : ''
}

const SHAPE_TEXT: Record<string, string> = {
  rect: '矩形',
  stadium: '起止',
  round: '圆角',
  diamond: '判断',
  circle: '圆点'
}

export function VisualizationView({
  viz,
  onCite
}: {
  viz: VizSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const flows = useMemo(() => viz.flows ?? [], [viz])
  const [tab, setTab] = useState<string>('timeline')
  const [asTable, setAsTable] = useState(false)

  const tlSorted = useMemo(
    () =>
      [...viz.timeline.nodes].sort((a, b) => {
        const da = a.date || '9999'
        const db = b.date || '9999'
        return da < db ? -1 : da > db ? 1 : 0
      }),
    [viz]
  )

  let body: JSX.Element | null = null
  if (tab === 'timeline') {
    body = asTable ? <TimelineTable nodes={tlSorted} onCite={onCite} /> : <TimelineRail nodes={tlSorted} onCite={onCite} />
  } else if (tab === 'relation') {
    body = asTable ? <RelationTable spec={viz} onCite={onCite} /> : <RelationView spec={viz} onCite={onCite} />
  } else if (tab.startsWith('flow:')) {
    const flow = flows[Number(tab.slice(5))]
    body = flow ? (asTable ? <FlowTable spec={flow} onCite={onCite} /> : <FlowView spec={flow} onCite={onCite} />) : null
  }

  return (
    <div className="viz" aria-label="可视化阅卷">
      <div className="viz-bar">
        <div className="viz-tabs" role="tablist" aria-label="图表类型">
          <button
            role="tab"
            aria-selected={tab === 'timeline'}
            className={`viz-tab${tab === 'timeline' ? ' on' : ''}`}
            onClick={() => setTab('timeline')}
          >
            <Icon id="i-clock" />
            时间线（{viz.timeline.nodes.length}）
          </button>
          <button
            role="tab"
            aria-selected={tab === 'relation'}
            className={`viz-tab${tab === 'relation' ? ' on' : ''}`}
            onClick={() => setTab('relation')}
          >
            <Icon id="i-user" />
            人物关系（{viz.relationGraph.nodes.length}）
          </button>
          {flows.map((f, i) => (
            <button
              key={i}
              role="tab"
              aria-selected={tab === `flow:${i}`}
              className={`viz-tab${tab === `flow:${i}` ? ' on' : ''}`}
              onClick={() => setTab(`flow:${i}`)}
            >
              <Icon id="i-chart" />
              {f.title}（{f.nodes.length}）
            </button>
          ))}
        </div>
        <button
          className="viz-toggle"
          title={asTable ? '切换为图形视图' : '切换为表格视图（图形不可用时的降级阅读，REQ-VIS-004）'}
          aria-pressed={asTable}
          onClick={() => setAsTable(!asTable)}
        >
          <Icon id={asTable ? 'i-chart' : 'i-list'} />
          {asTable ? '图形' : '表格'}
        </button>
      </div>

      {viz.warnings?.map((w) => (
        <div key={w} className="viz-warn">
          <Icon id="i-alert" />
          {w}
        </div>
      ))}

      {body}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 时间线（图形轨道 + 表格降级）
// ---------------------------------------------------------------------------

function TimelineRail({
  nodes,
  onCite
}: {
  nodes: TimelineNode[]
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  if (nodes.length === 0) {
    return <div className="viz-empty">材料中未提取到带日期的事件，可切换表格查看命中摘要。</div>
  }
  return (
    <div className="tl-rail" role="list" aria-label="事实时间线">
      {nodes.map((n, i) => (
        <div key={i} className={`tl-node${n.status === 'needs_human' ? ' warn' : ''}`} role="listitem">
          <div className="tl-date">{n.date || '时间待定'}</div>
          <div className="tl-card">
            <div className="tl-event">{n.event}</div>
            {n.actors.length > 0 && (
              <div className="tl-actors">
                {n.actors.map((a, j) => (
                  <span key={j} className="tl-actor">
                    {a}
                  </span>
                ))}
              </div>
            )}
            <CiteBadge cite={n.cite} onCite={onCite} />
          </div>
        </div>
      ))}
    </div>
  )
}

function TimelineTable({
  nodes,
  onCite
}: {
  nodes: TimelineNode[]
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  return (
    <table className="viz-table" aria-label="时间线数据表">
      <thead>
        <tr>
          <th>日期</th>
          <th>事件</th>
          <th>参与者</th>
          <th>引证</th>
        </tr>
      </thead>
      <tbody>
        {nodes.map((n, i) => (
          <tr key={i}>
            <td className="mono">{n.date || '—'}</td>
            <td>{n.event}</td>
            <td>{n.actors.join('、') || '—'}</td>
            <td>
              <CiteBadge cite={n.cite} onCite={onCite} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ---------------------------------------------------------------------------
// 人物关系图（mermaid 渲染 + 径向 SVG 降级 + 表格降级）
// ---------------------------------------------------------------------------

/** mermaid 优先，渲染失败回退径向 SVG（REQ-VIS-004 图形降级链） */
function RelationView({
  spec,
  onCite
}: {
  spec: VizSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const [legacy, setLegacy] = useState(false)
  useEffect(() => setLegacy(false), [spec])
  if (legacy || spec.relationGraph.nodes.length === 0) {
    return <RelationGraphView spec={spec} onCite={onCite} />
  }
  return <RelationMermaidView spec={spec} onCite={onCite} onFallback={() => setLegacy(true)} />
}

function RelationMermaidView({
  spec,
  onCite,
  onFallback
}: {
  spec: VizSpec
  onCite: (docId: string, pageNo: number) => void
  onFallback: () => void
}): JSX.Element {
  const { nodes, edges } = spec.relationGraph
  const [selName, setSelName] = useState<string | null>(null)
  useEffect(() => setSelName(null), [spec])

  // 结构化 → mermaid 最小结构：id=姓名，label 带身份，机构 stadium / 个人 round
  const mgNodes = useMemo<MgNode[]>(
    () =>
      nodes.map((n) => ({
        id: n.name,
        label: n.role ? `${n.name}（${n.role}）` : n.name,
        shape: n.kind === 'org' ? 'stadium' : 'round',
        kind: n.kind,
        status: n.status
      })),
    [nodes]
  )
  const mgEdges = useMemo<MgEdge[]>(
    () =>
      edges.map((e) => ({
        from: e.from,
        to: e.to,
        label: e.status === 'needs_human' ? `△ ${e.label}` : e.label,
        dashed: e.status === 'needs_human',
        status: e.status
      })),
    [edges]
  )

  const selNode = selName ? (nodes.find((n) => n.name === selName) ?? null) : null
  const relEdges = selNode ? edges.filter((e) => e.from === selNode.name || e.to === selNode.name) : []

  return (
    <div className="rg-wrap">
      <MermaidGraph
        nodes={mgNodes}
        edges={mgEdges}
        direction="LR"
        selectedId={selName}
        onSelectNode={setSelName}
        onRenderError={onFallback}
        ariaLabel={`人物关系图，${nodes.length} 个节点，${edges.length} 条关系`}
      />
      {selNode && (
        <div className="rg-detail" aria-label={`节点详情：${selNode.name}`}>
          <div className="rg-detail-head">
            <b>{selNode.name}</b>
            <span className="rr-role">{selNode.role || selNode.kind}</span>
            {selNode.note && <span className="rr-note">{selNode.note}</span>}
            {statusText(selNode.status) && <span className="rr-note">{statusText(selNode.status)}</span>}
          </div>
          <CiteBadge cite={selNode.cite} onCite={onCite} />
          {relEdges.length > 0 && (
            <div className="rg-rel-list">
              {relEdges.map((e, i) => (
                <div key={i} className="rg-rel">
                  {e.from === selNode.name ? `→ ${e.to}` : `← ${e.from}`}
                  <span className="rg-rel-label">{e.label}</span>
                  {statusText(e.status) && <span className="rr-note">{statusText(e.status)}</span>}
                  <CiteBadge cite={e.cite} onCite={onCite} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const RG_W = 640
const RG_H = 460

/** 径向 SVG 布局（mermaid 不可用时的图形降级） */
function RelationGraphView({
  spec,
  onCite
}: {
  spec: VizSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const { nodes, edges } = spec.relationGraph
  const [zoom, setZoom] = useState(1)
  const [sel, setSel] = useState<Selection>(null)

  const pos = useMemo(() => {
    const n = nodes.length
    const cx = RG_W / 2
    const cy = RG_H / 2
    const r = n <= 1 ? 0 : Math.min(RG_W, RG_H) / 2 - 78
    const map = new Map<string, { x: number; y: number }>()
    nodes.forEach((node, i) => {
      if (n === 1) {
        map.set(node.name, { x: cx, y: cy })
      } else if (n === 2) {
        map.set(node.name, i === 0 ? { x: cx - 130, y: cy } : { x: cx + 130, y: cy })
      } else {
        const angle = (2 * Math.PI * i) / n - Math.PI / 2
        map.set(node.name, { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) })
      }
    })
    return map
  }, [nodes])

  if (nodes.length === 0) {
    return <div className="viz-empty">未从材料中提取到人物或机构关系（本地降级模式不生成关系图）。</div>
  }

  const posOf = (name: string): { x: number; y: number } => pos.get(name) ?? { x: RG_W / 2, y: RG_H / 2 }
  const selName = sel?.kind === 'node' ? sel.node.name : null
  const relEdges =
    sel?.kind === 'node'
      ? edges.filter((e) => e.from === sel.node.name || e.to === sel.node.name)
      : []

  return (
    <div className="rg-wrap">
      <div className="rg-tools">
        <button className="rg-tool" aria-label="放大" title="放大" onClick={() => setZoom(Math.min(1.6, zoom + 0.15))}>
          <Icon id="i-plus" />
        </button>
        <span className="rg-zoom mono">{Math.round(zoom * 100)}%</span>
        <button className="rg-tool" aria-label="缩小" title="缩小" onClick={() => setZoom(Math.max(0.6, zoom - 0.15))}>
          <Icon id="i-minus" />
        </button>
        <span className="rg-hint">点击节点查看事实与来源</span>
      </div>
      <div className="rg-scroll">
        <svg
          width={RG_W * zoom}
          height={RG_H * zoom}
          viewBox={`0 0 ${RG_W} ${RG_H}`}
          role="img"
          aria-label={`人物关系图，${nodes.length} 个节点，${edges.length} 条关系`}
        >
          {edges.map((e, i) => {
            const a = posOf(e.from)
            const b = posOf(e.to)
            const active = sel?.kind === 'edge' && sel.edge === e
            const dim = selName !== null && e.from !== selName && e.to !== selName
            return (
              <g key={`e-${i}`} className={dim ? 'rg-edge dim' : 'rg-edge'} onClick={() => setSel({ kind: 'edge', edge: e })}>
                <title>{`${e.from} → ${e.to}：${e.label}`}</title>
                <line
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  className={`rg-line${e.status === 'needs_human' ? ' warn' : ''}${active ? ' active' : ''}`}
                />
                <text x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4} className="rg-elabel">
                  {e.label}
                </text>
              </g>
            )
          })}
          {nodes.map((n) => {
            const p = posOf(n.name)
            const active = selName === n.name
            const dim = selName !== null && !active && !edges.some((e) => (e.from === selName || e.to === selName) && (e.from === n.name || e.to === n.name))
            return (
              <g
                key={n.name}
                className={dim ? 'rg-node dim' : 'rg-node'}
                transform={`translate(${p.x}, ${p.y})`}
                onClick={() => setSel(active ? null : { kind: 'node', node: n })}
                role="button"
                aria-label={`${n.name}（${n.role || n.kind}）`}
              >
                <title>{`${n.name}（${n.role || n.kind}）${n.note ? ' · ' + n.note : ''}`}</title>
                <circle
                  r={22}
                  className={`rg-circle${n.kind === 'org' ? ' org' : ''}${n.status === 'needs_human' ? ' warn' : ''}${active ? ' active' : ''}`}
                />
                <text y={1} className="rg-initial">
                  {n.name.slice(0, 1)}
                </text>
                <text y={38} className="rg-nlabel">
                  {n.name.length > 6 ? `${n.name.slice(0, 6)}…` : n.name}
                </text>
                {n.role && (
                  <text y={51} className="rg-nrole">
                    {n.role.length > 8 ? `${n.role.slice(0, 8)}…` : n.role}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      </div>

      {sel?.kind === 'node' && (
        <div className="rg-detail" aria-label={`节点详情：${sel.node.name}`}>
          <div className="rg-detail-head">
            <b>{sel.node.name}</b>
            <span className="rr-role">{sel.node.role || sel.node.kind}</span>
            {sel.node.note && <span className="rr-note">{sel.node.note}</span>}
          </div>
          <CiteBadge cite={sel.node.cite} onCite={onCite} />
          {relEdges.length > 0 && (
            <div className="rg-rel-list">
              {relEdges.map((e, i) => (
                <button key={i} className="rg-rel" onClick={() => setSel({ kind: 'edge', edge: e })}>
                  {e.from === sel.node.name ? `→ ${e.to}` : `← ${e.from}`}
                  <span className="rg-rel-label">{e.label}</span>
                  <CiteBadge cite={e.cite} onCite={onCite} />
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {sel?.kind === 'edge' && (
        <div className="rg-detail" aria-label="关系详情">
          <div className="rg-detail-head">
            <b>{sel.edge.from}</b>
            <Icon id="i-arrow" />
            <b>{sel.edge.to}</b>
            <span className="rr-role">{sel.edge.label}</span>
          </div>
          <CiteBadge cite={sel.edge.cite} onCite={onCite} />
        </div>
      )}
    </div>
  )
}

function RelationTable({
  spec,
  onCite
}: {
  spec: VizSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const { nodes, edges } = spec.relationGraph
  return (
    <>
      <table className="viz-table" aria-label="人物机构表">
        <thead>
          <tr>
            <th>名称</th>
            <th>类型</th>
            <th>身份/角色</th>
            <th>备注</th>
            <th>引证</th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((n) => (
            <tr key={n.name}>
              <td>{n.name}</td>
              <td>{n.kind === 'org' ? '机构' : '个人'}</td>
              <td>{n.role || '—'}</td>
              <td>{n.note || '—'}</td>
              <td>
                <CiteBadge cite={n.cite} onCite={onCite} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {edges.length > 0 && (
        <table className="viz-table" aria-label="关系表">
          <thead>
            <tr>
              <th>从</th>
              <th>关系到</th>
              <th>标签</th>
              <th>引证</th>
            </tr>
          </thead>
          <tbody>
            {edges.map((e, i) => (
              <tr key={i}>
                <td>{e.from}</td>
                <td>{e.to}</td>
                <td>{e.label}</td>
                <td>
                  <CiteBadge cite={e.cite} onCite={onCite} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// 流程/证据关系图（mermaid 渲染 + 表格降级）
// ---------------------------------------------------------------------------

function FlowView({
  spec,
  onCite
}: {
  spec: FlowSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const [legacy, setLegacy] = useState(false)
  const [selId, setSelId] = useState<string | null>(null)
  useEffect(() => {
    setLegacy(false)
    setSelId(null)
  }, [spec])

  const mgNodes = useMemo<MgNode[]>(
    () => spec.nodes.map((n) => ({ id: n.id, label: n.label, shape: n.shape, status: n.status })),
    [spec]
  )
  const mgEdges = useMemo<MgEdge[]>(
    () =>
      spec.edges.map((e) => ({
        from: e.from,
        to: e.to,
        label: e.status === 'needs_human' && e.label ? `△ ${e.label}` : e.label,
        dashed: e.dashed || e.status === 'needs_human',
        status: e.status
      })),
    [spec]
  )

  if (legacy) return <FlowTable spec={spec} onCite={onCite} />

  const nameOf = (id: string): string => spec.nodes.find((n) => n.id === id)?.label ?? id
  const selNode = selId ? (spec.nodes.find((n) => n.id === selId) ?? null) : null
  const selEdges = selNode ? spec.edges.filter((e) => e.from === selNode.id || e.to === selNode.id) : []

  return (
    <div className="rg-wrap">
      <MermaidGraph
        nodes={mgNodes}
        edges={mgEdges}
        direction={spec.direction}
        selectedId={selId}
        onSelectNode={setSelId}
        onRenderError={() => setLegacy(true)}
        ariaLabel={`${spec.title}，${spec.nodes.length} 个节点，${spec.edges.length} 条流转`}
      />
      {selNode && (
        <div className="rg-detail" aria-label={`节点详情：${selNode.label}`}>
          <div className="rg-detail-head">
            <b>{selNode.label}</b>
            {statusText(selNode.status) && <span className="rr-role">{statusText(selNode.status)}</span>}
          </div>
          <CiteBadge cite={selNode.cite} onCite={onCite} />
          {selEdges.length > 0 && (
            <div className="rg-rel-list">
              {selEdges.map((e, i) => (
                <div key={i} className="rg-rel">
                  {e.from === selNode.id ? `→ ${nameOf(e.to)}` : `← ${nameOf(e.from)}`}
                  {e.label && <span className="rg-rel-label">{e.label}</span>}
                  {e.dashed && <span className="rr-note">虚线（推测/待核实路径）</span>}
                  {statusText(e.status) && <span className="rr-note">{statusText(e.status)}</span>}
                  <CiteBadge cite={e.cite} onCite={onCite} />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function FlowTable({
  spec,
  onCite
}: {
  spec: FlowSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const nameOf = (id: string): string => spec.nodes.find((n) => n.id === id)?.label ?? id
  return (
    <>
      <table className="viz-table" aria-label={`流程图数据表：${spec.title}`}>
        <thead>
          <tr>
            <th>节点</th>
            <th>形状</th>
            <th>状态</th>
            <th>引证</th>
          </tr>
        </thead>
        <tbody>
          {spec.nodes.map((n) => (
            <tr key={n.id}>
              <td>{n.label}</td>
              <td>{SHAPE_TEXT[n.shape ?? ''] ?? '矩形'}</td>
              <td>{statusText(n.status) || '已支持'}</td>
              <td>
                <CiteBadge cite={n.cite} onCite={onCite} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {spec.edges.length > 0 && (
        <table className="viz-table" aria-label="流转表">
          <thead>
            <tr>
              <th>从</th>
              <th>到</th>
              <th>流转</th>
              <th>状态</th>
              <th>引证</th>
            </tr>
          </thead>
          <tbody>
            {spec.edges.map((e, i) => (
              <tr key={i}>
                <td>{nameOf(e.from)}</td>
                <td>{nameOf(e.to)}</td>
                <td>
                  {e.label || '—'}
                  {e.dashed ? '（虚线）' : ''}
                </td>
                <td>{statusText(e.status) || '已支持'}</td>
                <td>
                  <CiteBadge cite={e.cite} onCite={onCite} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
