import { useCallback, useEffect, useState, type JSX } from 'react'
import type {
  AutomationCapability,
  AutomationRunResult,
  RecoveryCenterItem,
  ResolveToolCallAction,
  SubagentRun,
  ToolJournalEntry
} from '@lawbuddy/contracts'
import { errorMessage, getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { Icon } from './Icon'

const LIFECYCLE_LABEL: Record<string, string> = {
  CREATED: '已创建',
  PLANNING: '规划中',
  RUNNING: '执行中',
  WAITING_APPROVAL: '待审批',
  WAITING_HUMAN: '待人工确认',
  PAUSED: '已暂停',
  CANCELLING: '停止中',
  CANCELLED: '已取消',
  VERIFYING: '校验中',
  COMPLETED: '已完成',
  COMPLETED_WITH_WARNINGS: '完成（有警告）',
  FAILED: '失败'
}

const JOURNAL_LABEL: Record<string, string> = {
  staged: '已登记未执行',
  executing: '执行中',
  succeeded: '成功',
  failed_retryable: '失败（可重试）',
  failed_final: '失败（终态）',
  unknown: '副作用未知',
  cancelled: '已取消'
}

const RESOLVE_ACTION_LABEL: Record<ResolveToolCallAction, string> = {
  mark_happened: '标记已发生',
  re_execute: '重新执行',
  ignore: '忽略'
}

const RESOLVE_ACTION_HINT: Record<ResolveToolCallAction, string> = {
  mark_happened: '确认副作用已经发生：登记为成功，不再重放',
  re_execute: '确认副作用没有发生：允许后续重新执行该调用',
  ignore: '不再处理该调用（保留登记供审计）'
}

const SUBAGENT_STATUS_LABEL: Record<string, string> = {
  pending: '待启动',
  running: '执行中',
  staged: '结果暂存',
  accepted: '已采纳',
  rejected: '已驳回',
  failed: '失败',
  cancelled: '已取消'
}

/** 恢复中心（§7.1）：失败 / unknown / 待继续任务 + 技术状态 + 人工处置入口 */
export function RecoveryDialog(): JSX.Element | null {
  const { recoveryOpen, recoveryTab, setRecoveryOpen, currentCaseId, toast } = useApp()
  const [items, setItems] = useState<RecoveryCenterItem[]>([])
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [journal, setJournal] = useState<ToolJournalEntry[]>([])
  const [subagents, setSubagents] = useState<SubagentRun[]>([])
  const [automation, setAutomation] = useState<AutomationCapability | null>(null)
  const [automationResult, setAutomationResult] = useState<AutomationRunResult | null>(null)
  const [running, setRunning] = useState(false)
  const [busy, setBusy] = useState(false)

  const close = (): void => {
    setSelected(null)
    setJournal([])
    setSubagents([])
    setRecoveryOpen(false)
  }
  const cardRef = useDialogA11y(close, recoveryOpen)

  const reload = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge) return
    setLoading(true)
    try {
      const payload = await bridge.agent.recoveryCenter({ caseId: currentCaseId ?? undefined })
      setItems(payload.items)
    } catch (e) {
      toast(`读取恢复中心失败：${errorMessage(e)}`)
    } finally {
      setLoading(false)
    }
  }, [currentCaseId, toast])

  const loadDetail = useCallback(
    async (taskId: string) => {
      const bridge = getBridge()
      if (!bridge) return
      setSelected(taskId)
      try {
        const [entries, runs] = await Promise.all([
          bridge.agent.toolJournal({ taskId, limit: 200 }),
          bridge.agent.subagentRuns({ taskId, limit: 100 })
        ])
        setJournal(entries)
        setSubagents(runs)
      } catch (e) {
        toast(`读取任务状态失败：${errorMessage(e)}`)
      }
    },
    [toast]
  )

  useEffect(() => {
    if (recoveryOpen) void reload()
  }, [recoveryOpen, reload])

  const reloadAutomation = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      setAutomation(await bridge.automation.status())
    } catch (e) {
      toast(`读取自动化状态失败：${errorMessage(e)}`)
    }
  }, [toast])

  useEffect(() => {
    if (recoveryOpen && recoveryTab === 'automation') void reloadAutomation()
  }, [recoveryOpen, recoveryTab, reloadAutomation])

  const runAutomation = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    if (!currentCaseId) {
      toast('请先选择案件：本地巡检按案件范围执行')
      return
    }
    setRunning(true)
    try {
      const result = await bridge.automation.run({ caseId: currentCaseId })
      setAutomationResult(result)
      toast('本地巡检完成（只读，未变更任何状态）')
    } catch (e) {
      toast(`巡检失败：${errorMessage(e)}`)
    } finally {
      setRunning(false)
    }
  }

  const resolve = async (
    taskId: string,
    journalId: string,
    action: ResolveToolCallAction
  ): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      await bridge.agent.resolveToolCall({ taskId, journalId, action })
      toast(`已${RESOLVE_ACTION_LABEL[action]}`)
      await Promise.all([reload(), loadDetail(taskId)])
    } catch (e) {
      toast(`处置失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const resumeTask = async (taskId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      await bridge.agent.resume(taskId)
      toast('任务已从检查点继续执行')
      await reload()
    } catch (e) {
      toast(`继续失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const cancelTask = async (taskId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      await bridge.agent.cancel(taskId)
      toast('已请求停止任务')
      await reload()
    } catch (e) {
      toast(`停止失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  if (!recoveryOpen) return null

  const detail = items.find((i) => i.taskId === selected) ?? null
  const unresolved = journal.filter((e) => e.status === 'unknown')

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="恢复中心"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card recovery-card">
        <div className="modal-head">
          <h3>恢复中心</h3>
          <div className="recovery-head-right">
            <div className="viz-tabs" role="tablist" aria-label="恢复中心视图">
              <button
                role="tab"
                aria-selected={recoveryTab === 'recovery'}
                className={`viz-tab${recoveryTab === 'recovery' ? ' on' : ''}`}
                onClick={() => setRecoveryOpen(true, 'recovery')}
              >
                <Icon id="i-rotate" />
                恢复处置
              </button>
              <button
                role="tab"
                aria-selected={recoveryTab === 'subagents'}
                className={`viz-tab${recoveryTab === 'subagents' ? ' on' : ''}`}
                onClick={() => setRecoveryOpen(true, 'subagents')}
              >
                <Icon id="i-layers" />
                子代理进度
              </button>
              <button
                role="tab"
                aria-selected={recoveryTab === 'automation'}
                className={`viz-tab${recoveryTab === 'automation' ? ' on' : ''}`}
                onClick={() => setRecoveryOpen(true, 'automation')}
              >
                <Icon id="i-shield" />
                本地巡检
              </button>
            </div>
            <span className="audit-hint">仅显示技术状态 · 不含工具参数与原文</span>
            <button className="icon-btn" aria-label="刷新" title="刷新" onClick={() => void reload()}>
              <Icon id="i-new" />
            </button>
            <button className="icon-btn" aria-label="关闭" onClick={close}>
              <Icon id="i-x" />
            </button>
          </div>
        </div>

        <div className="modal-body recovery-body">
          {recoveryTab === 'automation' ? (
            <div className="conv-pane" aria-label="本地巡检">
              <div className="recovery-section">
                <div className="recovery-section-title">
                  <Icon id="i-shield" />
                  受限本地巡检
                </div>
                <div className="conv-hint">
                  只读检查：统计引语是否仍可定位、记忆是否因文档换版而失效、待人工核实项有多少。
                  <strong>不外发任何内容、不新建任务、不调用模型、不排后台定时任务</strong>；
                  任何状态变更（重新核验、更正记忆、批准提案）都需要你在界面中确认。
                </div>
                <div className="recovery-sub-row">
                  <span
                    className={`st-badge ${automation?.enabled ? 'st-favorable' : 'st-neutral'}`}
                  >
                    {automation?.enabled ? '已开启' : '未开启'}
                  </span>
                  <span className="cq-src none">
                    开关 <span className="mono">{automation?.settingKey ?? 'agent.localAutomation'}</span>
                    （默认关闭；可在设置中开启）
                  </span>
                </div>
                {!automation?.enabled && (
                  <div className="conv-hint">
                    未开启时巡检会被拒绝执行。开启方式：设置中把{' '}
                    <span className="mono">agent.localAutomation</span> 设为 true。
                  </div>
                )}
                <div className="cq-actions">
                  <button
                    className="btn-primary cq-btn"
                    disabled={running || !automation?.enabled || !currentCaseId}
                    onClick={() => void runAutomation()}
                  >
                    {running ? '巡检中…' : '执行一次本地巡检'}
                  </button>
                  <button className="btn-ghost cq-btn" onClick={() => void reloadAutomation()}>
                    刷新状态
                  </button>
                </div>
                {!currentCaseId && <div className="conv-hint">请先在左侧选择案件。</div>}
              </div>

              {automationResult && (
                <div className="recovery-section" aria-label="巡检结果">
                  <div className="recovery-section-title">
                    <Icon id="i-list" />
                    巡检结果
                  </div>
                  <div className="recovery-sub-row">
                    <span className="cq-src none">
                      {(automationResult.ranAt || '').slice(0, 19).replace('T', ' ')}
                    </span>
                  </div>
                  <div className="recovery-grid">
                    <div className="recovery-item">
                      <span className="recovery-line">
                        <span className="recovery-mode">过期/待核实 Claim</span>
                        <span className="st-badge st-neutral">
                          {automationResult.staleClaims}
                        </span>
                      </span>
                      <span className="recovery-line recovery-sub">
                        其中引语仍可定位、可重新核验恢复 {automationResult.recoverableClaims} 条
                      </span>
                    </div>
                    <div className="recovery-item">
                      <span className="recovery-line">
                        <span className="recovery-mode">待人工核实</span>
                        <span className="st-badge st-neutral">
                          {automationResult.needsHuman}
                        </span>
                      </span>
                      <span className="recovery-line recovery-sub">
                        需你逐项确认；系统不会自动处理
                      </span>
                    </div>
                    <div className="recovery-item">
                      <span className="recovery-line">
                        <span className="recovery-mode">失效记忆</span>
                        <span className="st-badge st-neutral">
                          {automationResult.staleMemories}
                        </span>
                      </span>
                      <span className="recovery-line recovery-sub">
                        源文档已失效但仍被当作有效事实的记忆{' '}
                        {automationResult.memoriesAtRisk} 条
                      </span>
                    </div>
                    <div className="recovery-item">
                      <span className="recovery-line">
                        <span className="recovery-mode">失效提案</span>
                        <span className="st-badge st-neutral">
                          {automationResult.staleProposals}
                        </span>
                      </span>
                      <span className="recovery-line recovery-sub">
                        请在「学习提案」中复核或回滚
                      </span>
                    </div>
                  </div>
                  <div className="conv-hint">{automationResult.note}</div>
                </div>
              )}
            </div>
          ) : loading && items.length === 0 ? (
            <div className="viz-empty">加载中…</div>
          ) : items.length === 0 ? (
            <div className="viz-empty">当前案件暂无可恢复或待处置的任务。</div>
          ) : (
            <div className="recovery-grid">
              <div className="recovery-list" aria-label="任务列表">
                {items.map((it) => (
                  <button
                    key={it.taskId}
                    className={`recovery-item${it.taskId === selected ? ' on' : ''}`}
                    onClick={() => void loadDetail(it.taskId)}
                  >
                    <span className="recovery-line">
                      <span className="recovery-mode">{it.mode}</span>
                      <span className={`st-badge ${lifecycleClass(it.lifecycle)}`}>
                        {LIFECYCLE_LABEL[it.lifecycle] ?? it.lifecycle}
                      </span>
                      {it.needsAttention && (
                        <span className="st-badge st-unfavorable" title="存在待处置或未完成的工具调用">
                          <Icon id="i-alert" />
                          需处置
                        </span>
                      )}
                    </span>
                    <span className="recovery-line recovery-sub">
                      <span className="mono">{it.taskId.slice(0, 8)}…</span>
                      <span>{it.runtimeVersion || '未登记'}</span>
                      <span>{(it.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
                    </span>
                    <span className="recovery-line recovery-sub">
                      <span>待处置 {it.unknownCount}</span>
                      {Object.entries(it.journal).map(([k, v]) => (
                        <span key={k}>
                          {JOURNAL_LABEL[k] ?? k} {v}
                        </span>
                      ))}
                    </span>
                  </button>
                ))}
              </div>

              <div className="recovery-detail" aria-label="任务详情">
                {!detail ? (
                  <div className="viz-empty">
                    {recoveryTab === 'subagents'
                      ? '选择左侧任务查看子代理运行进度。'
                      : '选择左侧任务查看工具调用与子代理状态。'}
                  </div>
                ) : (
                  <>
                    <div className="recovery-detail-head">
                      <span className="modal-sub">
                        运行时 {detail.runtimeVersion || '未登记'} · Journal{' '}
                        {Object.entries(detail.journal)
                          .map(([k, v]) => `${JOURNAL_LABEL[k] ?? k} ${v}`)
                          .join(' / ') || '无记录'}
                      </span>
                      <div className="recovery-actions">
                        {!(['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'CANCELLED', 'FAILED'] as string[]).includes(
                          detail.lifecycle
                        ) ? (
                          <button
                            className="btn-ghost"
                            disabled={busy}
                            onClick={() => void cancelTask(detail.taskId)}
                          >
                            停止任务
                          </button>
                        ) : (
                          <button
                            className="btn-ghost"
                            disabled={busy}
                            title="从最近检查点继续执行；已完成的工具调用不会重复"
                            onClick={() => void resumeTask(detail.taskId)}
                          >
                            继续执行
                          </button>
                        )}
                      </div>
                    </div>

                    {recoveryTab === 'recovery' && (
                      unresolved.length === 0 ? (
                        <div className="viz-empty">
                          没有副作用未知的调用。失败或中断的工具调用会在此处出现，处置后才能重试。
                        </div>
                      ) : (
                        <>
                          {unresolved.map((e) => (
                            <div className="cq-row" key={e.id}>
                              <div className="cq-main">
                                <div className="cq-badges">
                                  <span className="st-badge st-unfavorable">
                                    <Icon id="i-alert" />
                                    副作用未知
                                  </span>
                                  <span className="rr-role">{e.toolName}</span>
                                  <span className="st-badge st-neutral">{e.effect}</span>
                                  <span className="st-badge st-neutral">第 {e.attempt} 次</span>
                                  {e.errorCode && <span className="st-badge st-neutral">{e.errorCode}</span>}
                                </div>
                                <div className="cq-text">
                                  该调用在开始执行后中断，系统无法确认副作用是否已发生，因此不会自动重放。
                                  请查看工具目标后选择处置方式。
                                </div>
                                <div className="cq-meta">
                                  <span className="cq-time">
                                    {(e.updatedAt || '').slice(0, 16).replace('T', ' ')}
                                  </span>
                                  <span className="cq-src none">{e.callId}</span>
                                </div>
                              </div>
                              <div className="cq-actions">
                                {(['mark_happened', 're_execute', 'ignore'] as ResolveToolCallAction[]).map((a) => (
                                  <button
                                    key={a}
                                    className={a === 'mark_happened' ? 'btn-primary cq-btn' : 'btn-ghost cq-btn'}
                                    title={RESOLVE_ACTION_HINT[a]}
                                    disabled={busy}
                                    onClick={() => void resolve(detail.taskId, e.id, a)}
                                  >
                                    {RESOLVE_ACTION_LABEL[a]}
                                  </button>
                                ))}
                              </div>
                            </div>
                          ))}
                        </>
                      )
                    )}

                    {recoveryTab === 'subagents' && (
                    <div className="recovery-section" aria-label="子代理进度">
                      <div className="recovery-section-title">
                        <Icon id="i-layers" />
                        子代理进度
                      </div>
                      {subagents.length === 0 ? (
                        <div className="viz-empty">本任务没有子代理运行记录。</div>
                      ) : (
                        subagents.map((r) => (
                          <div className="recovery-sub-row" key={r.id}>
                            <span className="st-badge st-neutral">{r.kind}</span>
                            <span className={`st-badge ${subagentClass(r.status)}`}>
                              {SUBAGENT_STATUS_LABEL[r.status] ?? r.status}
                            </span>
                            <span className="mono">{r.roundsUsed} 轮</span>
                            <span className="mono">{r.toolCallsUsed} 次工具</span>
                            {typeof r.scope.question === 'string' && (
                              <span className="recovery-question" title={r.scope.question}>
                                {r.scope.question}
                              </span>
                            )}
                            {r.cancelReason && <span className="cq-src none">{r.cancelReason}</span>}
                          </div>
                        ))
                      )}
                    </div>
                    )}

                    {recoveryTab === 'recovery' && journal.length > 0 && (
                      <div className="recovery-section" aria-label="工具调用记录">
                        <div className="recovery-section-title">
                          <Icon id="i-list" />
                          工具调用记录
                        </div>
                        {journal.slice(0, 40).map((e) => (
                          <div className="recovery-sub-row" key={e.id}>
                            <span className={`st-badge ${journalClass(e.status)}`}>
                              {JOURNAL_LABEL[e.status] ?? e.status}
                            </span>
                            <span className="rr-role">{e.toolName}</span>
                            <span className="st-badge st-neutral">{e.effect}</span>
                            {e.errorCode && <span className="cq-src none">{e.errorCode}</span>}
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function lifecycleClass(lifecycle: string): string {
  if (lifecycle === 'FAILED') return 'st-unfavorable'
  if (lifecycle === 'COMPLETED' || lifecycle === 'COMPLETED_WITH_WARNINGS') return 'st-favorable'
  return 'st-neutral'
}

function journalClass(status: string): string {
  if (status === 'succeeded') return 'st-favorable'
  if (status === 'unknown' || status === 'failed_final') return 'st-unfavorable'
  return 'st-neutral'
}

function subagentClass(status: string): string {
  if (status === 'failed') return 'st-unfavorable'
  if (status === 'accepted' || status === 'staged') return 'st-favorable'
  return 'st-neutral'
}
