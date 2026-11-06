import { useEffect, useState, type JSX } from 'react'
import type { ClaimSummary } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { useApp } from '../state/store'
import { Icon } from './Icon'

const REASON_LABEL: Record<string, string> = {
  QUOTE_MISMATCH: '引语与原文不匹配',
  PAGE_AMBIGUOUS: '原文多页出现，页码需人工确认',
  LOW_OCR_CONFIDENCE: 'OCR 置信度低',
  OUT_OF_EVIDENCE_SCOPE: '超出已检索证据范围',
  WEAK_SUPPORT: '证据支撑不足',
  SOURCE_CONFLICT: '来源冲突',
  CONFLICT_REJECTED: '矛盾裁决未采纳',
  DOCUMENT_DELETED: '来源文档已删除',
  SOURCE_UNAVAILABLE: '来源不可用'
}

const TYPE_LABEL: Record<string, string> = {
  fact: '事实',
  law: '法律',
  inference: '推断',
  recommendation: '建议'
}

type QueueTab = 'pending' | 'stale' | 'conflict'

/**
 * 待核实队列（PRD 11.4 / R-004 / R-007 / R-008）：
 * - 待核实（needs_human）：确认（用户确认）/ 驳回（删除），可填意见 → 审计留痕；
 * - 已失效（stale）：重新校验（引语重验），来源已删除时明确不可恢复；
 * - 矛盾证据（conflict）：成对展示双侧说法，采纳一侧（对侧转待核实）或整体驳回。
 */
export function ClaimsQueueDialog(): JSX.Element | null {
  const {
    claimsQueueOpen,
    setClaimsQueueOpen,
    pendingClaims,
    staleClaims,
    conflictClaims,
    claimsLoading,
    loadClaimsQueue,
    verifyClaim,
    recheckClaim,
    resolveConflict,
    currentCaseId,
    documents,
    openCitation
  } = useApp()
  const [tab, setTab] = useState<QueueTab>('pending')
  const [notes, setNotes] = useState<Record<string, string>>({})

  useEffect(() => {
    if (claimsQueueOpen && currentCaseId) void loadClaimsQueue()
  }, [claimsQueueOpen, currentCaseId, loadClaimsQueue])

  const close = (): void => {
    setNotes({})
    setClaimsQueueOpen(false)
  }
  const cardRef = useDialogA11y(close, claimsQueueOpen)

  if (!claimsQueueOpen) return null

  const list = tab === 'pending' ? pendingClaims : tab === 'stale' ? staleClaims : conflictClaims

  // 来源引证跳转：定位到原文档对应页（打开文档标签并高亮位置）
  const openSource = (docId: string, pageNo: number): void => {
    close()
    openCitation(docId, pageNo)
  }

  // 文档 id → 名称映射（队列来源以可读文档名展示）
  const docNames: Record<string, string> = {}
  for (const d of documents) docNames[d.id] = d.name

  // 矛盾对侧 claim（同一 conflict 列表内按 conflictsWith 查找，用于展示对侧来源）
  const conflictById: Record<string, ClaimSummary> = {}
  for (const c of conflictClaims) conflictById[c.id] = c

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="待核实队列"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card claims-card">
        <div className="modal-head">
          <h3>待核实队列</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>

        <div className="modal-body">
          <div className="viz-bar">
            <div className="viz-tabs" role="tablist" aria-label="队列分组">
              <button
                role="tab"
                aria-selected={tab === 'pending'}
                className={`viz-tab${tab === 'pending' ? ' on' : ''}`}
                onClick={() => setTab('pending')}
              >
                <Icon id="i-alert" />
                待核实（{pendingClaims.length}）
              </button>
              <button
                role="tab"
                aria-selected={tab === 'conflict'}
                className={`viz-tab${tab === 'conflict' ? ' on' : ''}`}
                onClick={() => setTab('conflict')}
              >
                <Icon id="i-bulb" />
                矛盾证据（{conflictClaims.length}）
              </button>
              <button
                role="tab"
                aria-selected={tab === 'stale'}
                className={`viz-tab${tab === 'stale' ? ' on' : ''}`}
                onClick={() => setTab('stale')}
              >
                <Icon id="i-clock" />
                已失效（{staleClaims.length}）
              </button>
            </div>
            <button className="viz-toggle" title="刷新队列" onClick={() => void loadClaimsQueue()}>
              <Icon id="i-new" />
              刷新
            </button>
          </div>

          {claimsLoading ? (
            <div className="viz-empty">加载中…</div>
          ) : list.length === 0 ? (
            <div className="viz-empty">
              {tab === 'pending'
                ? '当前案件没有待人工核实的结论。'
                : tab === 'conflict'
                  ? '当前案件没有待裁决的矛盾证据。'
                  : '当前案件没有失效的引证。'}
            </div>
          ) : tab === 'conflict' ? (
            // 矛盾证据：按 conflictsWith 去重成对展示（每对取一条）
            list
              .filter((c) => !c.conflictsWith || c.id < c.conflictsWith)
              .map((c) => (
                <ConflictRow
                  key={c.id}
                  claim={c}
                  counterpart={c.conflictsWith ? conflictById[c.conflictsWith] : undefined}
                  docNames={docNames}
                  onOpenSource={openSource}
                  notes={notes}
                  setNotes={setNotes}
                  resolveConflict={resolveConflict}
                />
              ))
          ) : (
            list.map((c) => (
              <ClaimRow
                key={c.id}
                claim={c}
                tab={tab}
                docNames={docNames}
                onOpenSource={openSource}
                notes={notes}
                setNotes={setNotes}
                verifyClaim={verifyClaim}
                recheckClaim={recheckClaim}
              />
            ))
          )}
        </div>
      </div>
    </div>
  )
}

/** 来源引证列表：文档名 + 页码，点击跳转原文对应页（无来源时占位提示） */
function SourceChips({
  sources,
  docNames,
  onOpenSource
}: {
  sources: string[]
  docNames: Record<string, string>
  onOpenSource: (docId: string, pageNo: number) => void
}): JSX.Element {
  if (sources.length === 0) return <span className="cq-src none">无来源</span>
  return (
    <>
      {sources.map((s, i) => {
        const [docId, page] = s.split(':')
        const name = docNames[docId] ?? `${docId.slice(0, 8)}…`
        return (
          <button
            key={i}
            className="cq-src mono clickable"
            title={`${name} · 第${page || '?'}页（点击跳转原文）`}
            onClick={() => onOpenSource(docId, Number(page) || 1)}
          >
            <Icon id="i-doc" />
            {name}
            {page ? ` · 第${page}页` : ''}
          </button>
        )
      })}
    </>
  )
}

/** 矛盾裁决行：本侧 vs 对侧对照（双侧来源可跳转），采纳此侧或整体驳回（R-004） */
function ConflictRow({
  claim,
  counterpart,
  docNames,
  onOpenSource,
  notes,
  setNotes,
  resolveConflict
}: {
  claim: ClaimSummary
  counterpart?: ClaimSummary
  docNames: Record<string, string>
  onOpenSource: (docId: string, pageNo: number) => void
  notes: Record<string, string>
  setNotes: (n: Record<string, string>) => void
  resolveConflict: (id: string, d: 'confirmed' | 'rejected', note?: string) => Promise<void>
}): JSX.Element {
  return (
    <div className="cq-row" aria-label="矛盾证据项">
      <div className="cq-main">
        <div className="cq-badges">
          <span className={`rr-role`}>{TYPE_LABEL[claim.type] ?? claim.type}</span>
          <span className="st-badge st-unfavorable">矛盾证据</span>
          <span className="st-badge fst-pending" title={claim.reasonCode ?? ''}>
            {REASON_LABEL[claim.reasonCode ?? ''] ?? claim.reasonCode ?? '来源冲突'}
          </span>
        </div>
        <div className="rr-vs">
          <div className="rr-vs-sides">
            <div className="rr-side">
              {claim.text}
              <div className="cq-meta side-src">
                <SourceChips sources={claim.sources} docNames={docNames} onOpenSource={onOpenSource} />
              </div>
            </div>
            <span className="rr-vs-mark" aria-hidden="true">
              vs
            </span>
            <div className="rr-side b">
              {claim.counterpartText ?? '（对侧说法缺失）'}
              <div className="cq-meta side-src">
                <SourceChips
                  sources={counterpart?.sources ?? []}
                  docNames={docNames}
                  onOpenSource={onOpenSource}
                />
              </div>
            </div>
          </div>
        </div>
        <div className="cq-meta">
          <span className="cq-time">{(claim.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
        </div>
      </div>
      <div className="cq-actions">
        <input
          className="form-input cq-note"
          placeholder="裁决意见（选填，随审计留痕）"
          value={notes[claim.id] ?? ''}
          onChange={(e) => setNotes({ ...notes, [claim.id]: e.target.value })}
        />
        <button
          className="btn-primary cq-btn"
          title="采纳本侧说法：本侧转用户确认，对侧转待核实"
          onClick={() => void resolveConflict(claim.id, 'confirmed', notes[claim.id])}
        >
          采纳此侧
        </button>
        <button
          className="btn-ghost cq-btn deny"
          title="矛盾双方均不采纳（双条删除，审计留痕）"
          onClick={() => void resolveConflict(claim.id, 'rejected', notes[claim.id])}
        >
          整体驳回
        </button>
      </div>
    </div>
  )
}

function ClaimRow({
  claim,
  tab,
  docNames,
  onOpenSource,
  notes,
  setNotes,
  verifyClaim,
  recheckClaim
}: {
  claim: ClaimSummary
  tab: 'pending' | 'stale'
  docNames: Record<string, string>
  onOpenSource: (docId: string, pageNo: number) => void
  notes: Record<string, string>
  setNotes: (n: Record<string, string>) => void
  verifyClaim: (id: string, d: 'confirmed' | 'rejected', note?: string) => Promise<void>
  recheckClaim: (id: string) => Promise<void>
}): JSX.Element {
  return (
    <div className="cq-row" aria-label="待核实项">
      <div className="cq-main">
        <div className="cq-badges">
          <span className="rr-role">{TYPE_LABEL[claim.type] ?? claim.type}</span>
          <span className={`st-badge ${claim.status === 'stale' ? 'st-neutral' : 'fst-pending'}`}>
            {claim.status === 'stale' ? '已失效' : '待核实'}
          </span>
          {claim.reasonCode && (
            <span className="st-badge st-unfavorable" title={claim.reasonCode}>
              {REASON_LABEL[claim.reasonCode] ?? claim.reasonCode}
            </span>
          )}
        </div>
        <div className="cq-text">{claim.text}</div>
        <div className="cq-meta">
          <SourceChips sources={claim.sources} docNames={docNames} onOpenSource={onOpenSource} />
          <span className="cq-time">{(claim.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
        </div>
      </div>
      <div className="cq-actions">
        {tab === 'pending' ? (
          <>
            <input
              className="form-input cq-note"
              placeholder="核实意见（选填，随审计留痕）"
              value={notes[claim.id] ?? ''}
              onChange={(e) => setNotes({ ...notes, [claim.id]: e.target.value })}
            />
            <button
              className="btn-primary cq-btn"
              title="标记为用户确认（PRD 11.4 处理方式）"
              onClick={() => void verifyClaim(claim.id, 'confirmed', notes[claim.id])}
            >
              确认
            </button>
            <button
              className="btn-ghost cq-btn deny"
              title="驳回并从台账移除（审计留痕）"
              onClick={() => void verifyClaim(claim.id, 'rejected', notes[claim.id])}
            >
              驳回
            </button>
          </>
        ) : (
          <>
            <button
              className="btn-ghost cq-btn"
              title="重新校验引语（PRD 11.3：重新定位成功前不能保持已核实）"
              onClick={() => void recheckClaim(claim.id)}
            >
              重新校验
            </button>
            <button
              className="btn-ghost cq-btn"
              title="人工确认有效（写入审计）"
              onClick={() => void verifyClaim(claim.id, 'confirmed', notes[claim.id])}
            >
              人工确认
            </button>
          </>
        )}
      </div>
    </div>
  )
}
