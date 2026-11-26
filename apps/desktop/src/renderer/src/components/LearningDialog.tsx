import { useCallback, useEffect, useState, type JSX } from 'react'
import type { LearningProposal, SkillStagingEntry, SkillVersion } from '@lawbuddy/contracts'
import { errorMessage, getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { Icon } from './Icon'

type Tab = 'memory' | 'skills'

const KIND_LABEL: Record<string, string> = {
  case_memory: '案件记忆',
  user_profile: '用户偏好',
  skill_update: '技能更新',
  dedupe: '合并去重',
  archive: '归档建议'
}

const STATUS_LABEL: Record<string, string> = {
  pending: '待批准',
  approved: '已批准',
  rejected: '已驳回',
  stale: '基线已变',
  superseded: '已被取代'
}

/**
 * 学习提案（§7.4）：只展示提案与扫描结果，批准后才落地。
 *
 * - 未批准的提案不会进入 Prompt、召回或正式记忆（由 Core 侧门禁保证）；
 * - `stale` 表示基线哈希已变化，必须重审，不能覆盖新值；
 * - 技能暂存里的危险项被标记 `blocked`，不允许强制放行。
 */
export function LearningDialog(): JSX.Element | null {
  const { learningOpen, setLearningOpen, currentCaseId, toast } = useApp()
  const [tab, setTab] = useState<Tab>('memory')
  const [proposals, setProposals] = useState<LearningProposal[]>([])
  const [staging, setStaging] = useState<SkillStagingEntry[]>([])
  const [versions, setVersions] = useState<SkillVersion[]>([])
  const [activeSkillId, setActiveSkillId] = useState<string | null>(null)
  const [diffText, setDiffText] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const close = (): void => setLearningOpen(false)
  const cardRef = useDialogA11y(close, learningOpen)

  const reload = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const [p, s] = await Promise.all([
        bridge.learning.proposals({ caseId: currentCaseId ?? undefined, limit: 100 }),
        bridge.skill.staging({ limit: 100 })
      ])
      setProposals(p)
      setStaging(s)
    } catch (e) {
      toast(`学习提案读取失败：${errorMessage(e)}`)
    }
  }, [currentCaseId, toast])

  useEffect(() => {
    if (learningOpen) void reload()
  }, [learningOpen, reload])

  const decide = async (id: string, action: 'approve' | 'reject'): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      if (action === 'approve') {
        await bridge.learning.approve({ proposalId: id })
        toast('提案已批准并落地')
      } else {
        await bridge.learning.reject({ proposalId: id, note: '用户驳回' })
        toast('提案已驳回')
      }
      await reload()
    } catch (e) {
      // baseHash 冲突时 Core 抛错并把提案标为 stale：这里如实提示，不覆盖新值
      toast(
        `${action === 'approve' ? '批准' : '驳回'}失败：${errorMessage(e)}`
      )
      await reload()
    } finally {
      setBusy(false)
    }
  }

  const rollback = async (id: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      await bridge.learning.rollback({ proposalId: id, note: '用户回滚' })
      toast('已回滚该提案的影响')
      await reload()
    } catch (e) {
      toast(`回滚失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const approveStaging = async (id: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      const r = await bridge.skill.approveStaging({ stagingId: id, note: '用户在技能暂存批准' })
      toast(`技能已批准并生成新版本：${r.name}`)
      await reload()
      // 批准返回真实 skillId；用它可以查看版本历史（baseVersionId 是旧版本，不能当技能 ID）
      await showVersions(r.skillId)
    } catch (e) {
      toast(`批准失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const rejectStaging = async (id: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      await bridge.skill.rejectStaging({ stagingId: id, note: '用户在技能暂存驳回' })
      toast('已驳回暂存技能')
      await reload()
    } catch (e) {
      toast(`驳回失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const showVersions = async (skillId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      setVersions(await bridge.skill.versions({ skillId }))
      setActiveSkillId(skillId)
      setDiffText(null)
    } catch (e) {
      toast(`版本读取失败：${errorMessage(e)}`)
    }
  }

  const showDiff = async (skillId: string, from: number, to: number): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const d = await bridge.skill.diff({ skillId, fromVersion: from, toVersion: to })
      setDiffText(d.diff)
    } catch (e) {
      toast(`diff 失败：${errorMessage(e)}`)
    }
  }

  if (!learningOpen) return null

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="学习提案"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card recovery-card">
        <div className="modal-head">
          <h3>学习提案</h3>
          <div className="recovery-head-right">
            <div className="viz-tabs" role="tablist" aria-label="学习提案视图">
              <button
                role="tab"
                aria-selected={tab === 'memory'}
                className={`viz-tab${tab === 'memory' ? ' on' : ''}`}
                onClick={() => setTab('memory')}
              >
                记忆提案
              </button>
              <button
                role="tab"
                aria-selected={tab === 'skills'}
                className={`viz-tab${tab === 'skills' ? ' on' : ''}`}
                onClick={() => setTab('skills')}
              >
                技能暂存与版本
              </button>
            </div>
            <span className="audit-hint">未批准不生效 · 危险项不可强装</span>
            <button className="icon-btn" aria-label="刷新" title="刷新" onClick={() => void reload()}>
              <Icon id="i-new" />
            </button>
            <button className="icon-btn" aria-label="关闭" onClick={close}>
              <Icon id="i-x" />
            </button>
          </div>
        </div>

        <div className="modal-body recovery-body">
          <div className="conv-pane">
            {tab === 'memory' ? (
              proposals.length === 0 ? (
                <div className="viz-empty">暂无可审阅的学习提案。</div>
              ) : (
                proposals.map((p) => (
                  <div className="cq-row" key={p.id}>
                    <div className="cq-main">
                      <div className="cq-badges">
                        <span className="rr-role">{KIND_LABEL[p.proposalKind] ?? p.proposalKind}</span>
                        <span className={`st-badge ${proposalClass(p.status)}`}>
                          {STATUS_LABEL[p.status] ?? p.status}
                        </span>
                        <span className="st-badge st-neutral">{p.targetKey}</span>
                        {p.status === 'stale' && (
                          <span className="st-badge st-unfavorable">
                            <Icon id="i-alert" />
                            基线已变，需重审
                          </span>
                        )}
                      </div>
                      <div className="cq-text">{p.rationale || '（无说明）'}</div>
                      <div className="cq-meta">
                        <span className="cq-src none">{p.createdAt.slice(0, 16).replace('T', ' ')}</span>
                        {Object.entries(p.source).map(([k, v]) => (
                          <span className="cq-src none" key={k}>
                            {k}: {String(v).slice(0, 40)}
                          </span>
                        ))}
                      </div>
                      {Object.keys(p.scan).length > 0 && (
                        <div className="cq-meta">
                          <span className="cq-src none">
                            扫描：{Array.isArray((p.scan as { rulesHit?: string[] }).rulesHit)
                              ? ((p.scan as { rulesHit?: string[] }).rulesHit ?? []).join(' / ') || '无规则命中'
                              : '已完成'}
                          </span>
                        </div>
                      )}
                      <details className="learn-diff">
                        <summary>查看变更 diff</summary>
                        <pre className="skill-out">{JSON.stringify(p.diff, null, 2)}</pre>
                      </details>
                    </div>
                    <div className="cq-actions">
                      {p.status === 'pending' && (
                        <>
                          <button
                            className="btn-primary cq-btn"
                            disabled={busy}
                            onClick={() => void decide(p.id, 'approve')}
                          >
                            批准
                          </button>
                          <button
                            className="btn-ghost cq-btn deny"
                            disabled={busy}
                            onClick={() => void decide(p.id, 'reject')}
                          >
                            驳回
                          </button>
                        </>
                      )}
                      {p.status === 'approved' && (
                        <button
                          className="btn-ghost cq-btn"
                          disabled={busy}
                          title="撤销该提案带来的变更"
                          onClick={() => void rollback(p.id)}
                        >
                          回滚
                        </button>
                      )}
                    </div>
                  </div>
                ))
              )
            ) : staging.length === 0 && versions.length === 0 ? (
              <div className="viz-empty">技能暂存区为空。</div>
            ) : (
              <>
                {staging.map((s) => (
                  <div className="cq-row" key={s.id}>
                    <div className="cq-main">
                      <div className="cq-badges">
                        <span className="rr-role">{s.name}</span>
                        <span
                          className={`st-badge ${
                            s.status === 'blocked'
                              ? 'st-unfavorable'
                              : s.status === 'approved'
                                ? 'st-favorable'
                                : 'st-neutral'
                          }`}
                        >
                          {s.status === 'blocked' ? '危险项已阻断' : s.status}
                        </span>
                        <span className="st-badge st-neutral">{(s.allowedTools ?? []).length} 个工具</span>
                      </div>
                      <div className="cq-text">{s.description || '（无描述）'}</div>
                      <div className="cq-meta">
                        <span className="cq-src none">sha:{s.contentSha256.slice(0, 12)}</span>
                        <span className="cq-src none">{s.sourceOrigin}</span>
                        {s.blockReason && <span className="cq-src none">{s.blockReason}</span>}
                      </div>
                      {Array.isArray((s.scan as { rulesHit?: string[] }).rulesHit) &&
                        ((s.scan as { rulesHit?: string[] }).rulesHit ?? []).length > 0 && (
                          <div className="cq-meta">
                            <span className="cq-src none">
                              命中规则：{((s.scan as { rulesHit?: string[] }).rulesHit ?? []).join(' / ')}
                            </span>
                          </div>
                        )}
                    </div>
                    <div className="cq-actions">
                      {s.status === 'pending' && (
                        <>
                          <button
                            className="btn-primary cq-btn"
                            disabled={busy}
                            onClick={() => void approveStaging(s.id)}
                          >
                            批准为新版本
                          </button>
                          <button
                            className="btn-ghost cq-btn deny"
                            disabled={busy}
                            onClick={() => void rejectStaging(s.id)}
                          >
                            驳回
                          </button>
                        </>
                      )}
                      {s.status === 'approved' && activeSkillId && (
                        <button className="btn-ghost cq-btn" onClick={() => void showVersions(activeSkillId)}>
                          查看版本
                        </button>
                      )}
                    </div>
                  </div>
                ))}

                {versions.length > 0 && (
                  <div className="recovery-section" aria-label="技能版本">
                    <div className="recovery-section-title">
                      <Icon id="i-list" />
                      版本历史
                    </div>
                    {versions.map((v) => (
                      <div className="recovery-sub-row" key={v.versionId}>
                        <span className="st-badge st-neutral">v{v.versionNo}</span>
                        {v.active && <span className="st-badge st-favorable">当前生效</span>}
                        <span className="cq-src none">sha:{v.contentSha256.slice(0, 12)}</span>
                        <span className="cq-src none">{v.createdAt.slice(0, 16).replace('T', ' ')}</span>
                        {v.note && <span className="cq-src none">{v.note}</span>}
                        <button
                          className="btn-ghost btn-mini"
                          onClick={() =>
                            void showDiff(v.skillId, Math.max(1, v.versionNo - 1), v.versionNo)
                          }
                        >
                          对比上一版
                        </button>
                      </div>
                    ))}
                    {diffText !== null && <pre className="skill-out">{diffText || '（无差异）'}</pre>}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function proposalClass(status: string): string {
  if (status === 'approved') return 'st-favorable'
  if (status === 'stale' || status === 'superseded') return 'st-unfavorable'
  return 'st-neutral'
}
