import { useState, type JSX } from 'react'
import type {
  CiteChip,
  ContractBody,
  ContractReviewPayload,
  ContractRiskItem,
  ContractWorkCopy,
  TextDiffResult
} from '@lawbuddy/contracts'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { CiteBadge } from './CiteBadge'
import { DiffPanel } from './DiffPanel'
import { Icon } from './Icon'

const LEVEL_LABEL: Record<string, string> = {
  severe: '严重',
  high: '高',
  medium: '中',
  low: '低'
}

const CATEGORY_LABEL: Record<string, string> = {
  business: '商务',
  legal: '法律',
  completeness: '完备性'
}

const DECISION_LABEL: Record<string, string> = {
  pending: '待处理',
  accepted: '已接受',
  rejected: '已拒绝',
  edited: '已改写'
}

const APPETITE_LABEL: Record<string, string> = {
  conservative: '保守',
  balanced: '均衡',
  aggressive: '进取'
}

/**
 * 合同审查结果（PRD 14.4 输出；DEV-MOD-005/006）：
 * 风险统计 + 风险卡片（等级/条款位置/原文/说明/影响/建议/替换文本/依据/引证），
 * 逐项接受 / 改写 / 拒绝，撤销 / 重做（REQ-CON-006/007：只写工作副本，操作可撤销）。
 * 消息流内由 body.contract 初始化，reviewId 存在时可拉取完整载荷交互。
 */
export function ContractReviewView({
  body,
  reviewId
}: {
  body: ContractBody
  /** 消息流内为空；有 reviewId 才能做决策交互 */
  reviewId?: string
}): JSX.Element {
  const { toast, openCitation } = useApp()
  const writable = useApp((s) => {
    const current = [...s.cases, ...s.archivedCases].find((c) => c.id === s.currentCaseId)
    return current?.defaultMode === 'contract_review' && current.status === 'active'
  })
  const [payload, setPayload] = useState<ContractReviewPayload | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  const [busy, setBusy] = useState(false)
  // 工作副本版本链与版本比较（DEV-MOD-009 / REQ-CON-002：原文、AI 建议、最终稿可区分）
  const [workCopies, setWorkCopies] = useState<ContractWorkCopy[] | null>(null)
  const [diff, setDiff] = useState<TextDiffResult | null>(null)

  type RiskRow = ContractRiskItem & { cite?: CiteChip }
  const risks: RiskRow[] = payload?.risks ?? body.risks.map((r, i): RiskRow => ({
    id: `body-${i}`,
    reviewId: body.reviewId ?? '',
    claimId: r.claimId ?? null,
    clauseId: null,
    riskLevel: r.riskLevel,
    riskLevelLabel: LEVEL_LABEL[r.riskLevel] ?? r.riskLevel,
    category: r.category,
    clauseRef: r.clauseRef,
    originalText: r.originalText,
    riskNote: r.riskNote,
    impact: r.impact,
    suggestion: r.suggestion,
    replacementText: r.replacementText,
    basis: r.basis,
    confidence: r.confidence,
    decision: 'pending',
    editedText: null,
    decidedAt: null
  }))

  const stats = payload?.review.riskStats ?? {
    total: body.riskStats.total,
    severe: body.riskStats.severe,
    accepted: 0,
    rejected: 0,
    pending: body.riskStats.pending
  }
  const rid = payload?.review.id ?? body.reviewId ?? reviewId

  const run = async (fn: () => Promise<ContractReviewPayload>): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      setPayload(await fn())
    } catch (e) {
      toast(`操作失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const load = (): Promise<ContractReviewPayload> => {
    const bridge = getBridge()
    if (!bridge || !rid) return Promise.reject(new Error('审查会话不可用'))
    return bridge.contract.getReview(rid)
  }

  const decide = (risk: ContractRiskItem, decision: 'accept' | 'reject' | 'edit', editedText?: string): void => {
    const bridge = getBridge()
    if (!bridge || !rid) {
      toast('任务完成后可进行接受 / 拒绝操作')
      return
    }
    void run(() => bridge.contract.decideRisk({ riskId: realId(risk), decision, editedText }))
  }

  /** 消息流快照项没有真实 id：先拉载荷，按条款位置对齐后再决策 */
  const realId = (risk: ContractRiskItem): string => {
    if (!risk.id.startsWith('body-')) return risk.id
    const found = (payload?.risks ?? []).find(
      (x) => x.clauseRef === risk.clauseRef && x.riskNote === risk.riskNote
    )
    if (!found) throw new Error('风险项已变更，请刷新后重试')
    return found.id
  }

  const prepare = async (risk: ContractRiskItem): Promise<ContractRiskItem | null> => {
    if (!payload && rid) await run(load)
    if (!risk.id.startsWith('body-')) return risk
    const found = (payload?.risks ?? []).find(
      (x) => x.clauseRef === risk.clauseRef && x.riskNote === risk.riskNote
    )
    if (!found) {
      toast('风险项已变更，请刷新后重试')
      return null
    }
    return found
  }

  // —— 工作副本版本链与版本比较（REQ-CON-002） ——
  const loadWorkCopies = (): void => {
    const bridge = getBridge()
    if (!bridge || !rid) {
      toast('任务完成后可查看工作副本版本')
      return
    }
    void bridge.contract
      .listWorkCopies(rid)
      .then(setWorkCopies)
      .catch((e) => toast(`版本加载失败：${e instanceof Error ? e.message : String(e)}`))
  }

  const compareWorkCopy = (fromVersionNo: number, toVersionNo?: number): void => {
    const bridge = getBridge()
    if (!bridge || !rid) return
    void bridge.contract
      .diffWorkCopies({ reviewId: rid, fromVersionNo, toVersionNo: toVersionNo ?? null })
      .then(setDiff)
      .catch((e) => toast(`版本比较失败：${e instanceof Error ? e.message : String(e)}`))
  }

  return (
    <div className="cr" aria-label="合同审查结果">
      {!writable && <p className="cr-audit">历史审查结果只读保留，当前任务不能修改此模式产物。</p>}
      <div className="cr-head">
        <span className="cr-title">
          <Icon id="i-doc" />
          合同审查{body.contractType ? ` · ${body.contractType}` : ''}
        </span>
        <span className="cr-stats">
          共 {stats.total} 项风险
          {stats.severe > 0 && <b className="st-badge st-unfavorable">严重 {stats.severe}</b>}
          <b className="st-badge st-neutral">待处理 {stats.pending}</b>
          {stats.accepted > 0 && <b className="st-badge st-favorable">已接受 {stats.accepted}</b>}
        </span>
        {rid && (
          <span className="cr-actions">
            <button className="cr-btn" disabled={busy || !writable} onClick={() => void run(() => bridgeUndo(rid))}>
              撤销
            </button>
            <button className="cr-btn" disabled={busy || !writable} onClick={() => void run(() => bridgeRedo(rid))}>
              重做
            </button>
            <button className="cr-btn" disabled={busy} onClick={() => void run(load)}>
              刷新
            </button>
          </span>
        )}
      </div>

      {risks.length === 0 && <div className="cr-empty">未发现风险项</div>}

      {risks.map((r, i) => (
        <div key={`${r.id}-${i}`} className={`cr-risk lv-${r.riskLevel}`}>
          <div className="cr-risk-head">
            <b className={`st-badge lvb-${r.riskLevel}`}>{LEVEL_LABEL[r.riskLevel] ?? r.riskLevel}风险</b>
            <span className="st-badge st-neutral">{CATEGORY_LABEL[r.category] ?? r.category}</span>
            {r.clauseRef && <span className="cr-loc">{r.clauseRef}</span>}
            {r.clausePage != null && <span className="cr-loc">第 {r.clausePage} 页</span>}
            <span className="cr-conf">{r.confidence === 'verified' ? '已核验' : `置信 ${r.confidence}`}</span>
            {payload && r.decision !== 'pending' && (
              <span className={`st-badge ${r.decision === 'rejected' ? 'st-neutral' : 'st-favorable'}`}>
                {DECISION_LABEL[r.decision] ?? r.decision}
              </span>
            )}
          </div>

          {r.originalText && <blockquote className="cr-quote">{r.originalText}</blockquote>}
          <div className="cr-note">{r.riskNote}</div>
          {r.impact && <div className="cr-line">影响：{r.impact}</div>}
          {r.suggestion && <div className="cr-line">建议：{r.suggestion}</div>}
          {r.replacementText && (
            <div className="cr-repl">
              <span>建议替换为</span>
              <pre>{r.replacementText}</pre>
            </div>
          )}
          {r.basis && <div className="cr-basis">依据：{r.basis}</div>}

          {r.cite && (
            <CiteBadge
              cite={r.cite}
              onCite={(docId, pageNo) => openCitation(docId, pageNo)}
              onPending={() => {}}
            />
          )}

          {payload && writable && (
            <div className="cr-op">
              {r.decision === 'pending' && (
                <>
                  <button className="cr-btn ok" disabled={busy} onClick={() => decide(r, 'accept')}>
                    接受修改
                  </button>
                  <button
                    className="cr-btn"
                    disabled={busy}
                    onClick={() => {
                      void prepare(r).then((it) => {
                        if (!it) return
                        setEditing(it.id)
                        setEditText(it.replacementText ?? it.suggestion ?? '')
                      })
                    }}
                  >
                    改写后接受
                  </button>
                  <button className="cr-btn" disabled={busy} onClick={() => decide(r, 'reject')}>
                    拒绝
                  </button>
                </>
              )}
              {r.decision !== 'pending' && (
                <button
                  className="cr-btn"
                  disabled={busy}
                  onClick={() => {
                    void prepare(r).then((it) => {
                      if (!it) return
                      setEditing(it.id)
                      setEditText(it.editedText ?? it.replacementText ?? '')
                    })
                  }}
                >
                  重新编辑
                </button>
              )}
            </div>
          )}

          {editing === r.id && (
            <div className="cr-edit">
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                placeholder="改写替换文本（应用到工作副本）"
                rows={3}
              />
              <div className="cr-edit-ops">
                <button
                  className="cr-btn ok"
                  disabled={busy || !editText.trim()}
                  onClick={() => {
                    setEditing(null)
                    decide(r, 'edit', editText.trim())
                  }}
                >
                  提交改写
                </button>
                <button className="cr-btn" onClick={() => setEditing(null)}>
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      ))}

      {payload?.workCopy && (
        <div className="cr-workcopy">
          <span>
            工作副本 v{payload.workCopy.versionNo}（已应用 {payload.workCopy.appliedRiskIds.length} 项修改，原件不变）
          </span>
          <button className="cr-btn" disabled={busy} onClick={loadWorkCopies}>
            版本历史与比较
          </button>
        </div>
      )}

      {/* —— 工作副本版本链（REQ-CON-002：与原件/上一版本比较，原件只读） —— */}
      {workCopies && (
        <div className="cr-wc-versions" role="region" aria-label="工作副本版本">
          <div className="cr-wc-head">
            工作副本版本（{workCopies.length}）
            <button className="icon-btn cr-wc-x" aria-label="关闭版本列表" onClick={() => setWorkCopies(null)}>
              <Icon id="i-x" />
            </button>
          </div>
          {workCopies.length === 0 ? (
            <div className="cr-empty">尚无工作副本版本（接受或改写风险项后生成）</div>
          ) : (
            workCopies.map((w, i) => (
              <div key={w.versionNo} className="cr-wc-row">
                <b>v{w.versionNo}</b>
                <span>{(w.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
                <span>已应用 {w.appliedRiskIds.length} 项</span>
                <button className="cr-btn" disabled={busy} onClick={() => compareWorkCopy(0, w.versionNo)}>
                  与原件比较
                </button>
                {i + 1 < workCopies.length && (
                  <button
                    className="cr-btn"
                    disabled={busy}
                    onClick={() => compareWorkCopy(workCopies[i + 1].versionNo, w.versionNo)}
                  >
                    与上一版本比较
                  </button>
                )}
              </div>
            ))
          )}
        </div>
      )}

      {diff && <DiffPanel diff={diff} onClose={() => setDiff(null)} />}
    </div>
  )
}

// undo/redo 经 bridge 直连（独立函数避免 hooks 依赖）
function bridgeUndo(reviewId: string): Promise<ContractReviewPayload> {
  const bridge = getBridge()
  if (!bridge) return Promise.reject(new Error('桥不可用'))
  return bridge.contract.undo(reviewId)
}

function bridgeRedo(reviewId: string): Promise<ContractReviewPayload> {
  const bridge = getBridge()
  if (!bridge) return Promise.reject(new Error('桥不可用'))
  return bridge.contract.redo(reviewId)
}
