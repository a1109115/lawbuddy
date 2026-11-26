import { useCallback, useEffect, useState, type JSX } from 'react'
import type {
  ArtifactSnapshot,
  ConversationListItem,
  ConversationSearchHit,
  ConversationStats
} from '@lawbuddy/contracts'
import { errorMessage, getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { Icon } from './Icon'

type Tab = 'search' | 'list' | 'snapshots'

/**
 * 会话管理（§7.2）：检索真实消息、分支、彻底删除、产物快照与回滚。
 *
 * - 检索结果来自 FTS/LIKE 命中的真实消息，附定位锚点（不是 AI 摘要）；
 * - 分支只复制消息文本，不复制任务/审批/预算；
 * - 彻底删除无软删除，删除前 Core 会先取消该会话下活动任务；
 * - 快照只覆盖工作副本与交付物，回滚绝不写案件原件。
 */
export function ConversationDialog(): JSX.Element | null {
  const { conversationOpen, setConversationOpen, currentCaseId, toast } = useApp()
  const [tab, setTab] = useState<Tab>('search')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<ConversationSearchHit[]>([])
  const [conversations, setConversations] = useState<ConversationListItem[]>([])
  const [stats, setStats] = useState<ConversationStats | null>(null)
  const [snapshots, setSnapshots] = useState<ArtifactSnapshot[]>([])
  const [targetPath, setTargetPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [searched, setSearched] = useState(false)

  const close = (): void => setConversationOpen(false)
  const cardRef = useDialogA11y(close, conversationOpen)

  const reload = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge || !currentCaseId) return
    try {
      const [list, st, snaps] = await Promise.all([
        bridge.conversation.list({ caseId: currentCaseId, limit: 100 }),
        bridge.conversation.stats({ caseId: currentCaseId }),
        bridge.conversation.snapshots({ caseId: currentCaseId, limit: 100 })
      ])
      setConversations(list)
      setStats(st)
      setSnapshots(snaps)
    } catch (e) {
      toast(`会话数据读取失败：${errorMessage(e)}`)
    }
  }, [currentCaseId, toast])

  useEffect(() => {
    if (conversationOpen) void reload()
  }, [conversationOpen, reload])

  const runSearch = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge || !currentCaseId) return
    if (!query.trim()) {
      toast('请输入检索词')
      return
    }
    setBusy(true)
    try {
      setHits(await bridge.conversation.search({ caseId: currentCaseId, query: query.trim(), limit: 40 }))
      setSearched(true)
    } catch (e) {
      toast(`检索失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const branch = async (conversationId: string, messageId?: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      const r = await bridge.conversation.branch({ conversationId, anchorMessageId: messageId })
      toast(`已从该处分支新会话（${r.messageCount} 条消息，不含授权与预算）`)
      await reload()
    } catch (e) {
      toast(`分支失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (conversationId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      const r = await bridge.conversation.delete({ conversationId })
      toast(
        `已彻底删除：${r.deletedMessages} 条消息` +
          (r.cancelledTaskIds.length ? `，同时取消 ${r.cancelledTaskIds.length} 个活动任务` : '')
      )
      setHits((h) => h.filter((x) => x.conversationId !== conversationId))
      await reload()
    } catch (e) {
      toast(`删除失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const rollback = async (snapshot: ArtifactSnapshot): Promise<void> => {
    const bridge = getBridge()
    if (!bridge || !currentCaseId) return
    if (!targetPath.trim()) {
      toast('请填写工作空间内的目标相对路径')
      return
    }
    setBusy(true)
    try {
      const r = await bridge.conversation.rollback({
        snapshotId: snapshot.snapshotId,
        caseId: currentCaseId,
        targetPath: targetPath.trim()
      })
      toast(`已回滚到 ${r.target}（${r.bytes ?? 0} 字节）`)
    } catch (e) {
      toast(`回滚失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  if (!conversationOpen) return null

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="会话管理"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card recovery-card">
        <div className="modal-head">
          <h3>会话管理</h3>
          <div className="recovery-head-right">
            <div className="viz-tabs" role="tablist" aria-label="会话管理视图">
              {([
                ['search', '检索对话'],
                ['list', '会话与分支'],
                ['snapshots', '产物快照']
              ] as Array<[Tab, string]>).map(([key, label]) => (
                <button
                  key={key}
                  role="tab"
                  aria-selected={tab === key}
                  className={`viz-tab${tab === key ? ' on' : ''}`}
                  onClick={() => setTab(key)}
                >
                  {label}
                </button>
              ))}
            </div>
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
            {!currentCaseId ? (
              <div className="viz-empty">请先选择案件：会话检索与快照均以案件为边界。</div>
            ) : tab === 'search' ? (
              <>
                <div className="conv-search-bar">
                  <input
                    className="form-input"
                    value={query}
                    placeholder="检索本案件对话原文（支持中文，命中真实消息）"
                    aria-label="会话检索词"
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void runSearch()
                    }}
                  />
                  <button className="btn-primary cq-btn" disabled={busy} onClick={() => void runSearch()}>
                    检索
                  </button>
                </div>
                <div className="conv-hint">
                  只返回本案件内命中原文与定位锚点；跨案结果在 SQL 层直接过滤。共{' '}
                  {stats?.messages ?? 0} 条消息、{stats?.indexedMessages ?? 0} 条已索引。
                </div>
                {hits.length === 0 ? (
                  <div className="viz-empty">{searched ? '没有命中的消息。' : '输入关键词后回车检索。'}</div>
                ) : (
                  hits.map((h) => (
                    <div className="cq-row" key={h.messageId}>
                      <div className="cq-main">
                        <div className="cq-badges">
                          <span className={`st-badge ${h.role === 'user' ? 'st-neutral' : 'st-favorable'}`}>
                            {h.role === 'user' ? '用户' : '助手'}
                          </span>
                          <span className="cq-src none">{h.createdAt.slice(0, 16).replace('T', ' ')}</span>
                        </div>
                        <div className="cq-text">{h.snippet}</div>
                        <div className="cq-meta">
                          <span className="cq-src none">{h.conversationId.slice(0, 8)}…</span>
                        </div>
                      </div>
                      <div className="cq-actions">
                        <button
                          className="btn-ghost cq-btn"
                          disabled={busy}
                          title="从该消息分支出新会话（只复制消息，不复制授权与预算）"
                          onClick={() => void branch(h.conversationId, h.messageId)}
                        >
                          从此处分支
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </>
            ) : tab === 'list' ? (
              conversations.length === 0 ? (
                <div className="viz-empty">本案件暂无会话记录。</div>
              ) : (
                conversations.map((c) => (
                  <div className="cq-row" key={c.id}>
                    <div className="cq-main">
                      <div className="cq-badges">
                        <span className="rr-role">{c.id.slice(0, 8)}…</span>
                        <span className="st-badge st-neutral">{c.messageCount} 条消息</span>
                        {c.isBranch && (
                          <span className="st-badge st-neutral" title="由其他会话分支而来">
                            分支自 {String(c.parentConversationId ?? '').slice(0, 8)}…
                          </span>
                        )}
                      </div>
                      <div className="cq-meta">
                        <span className="cq-src none">{c.createdAt.slice(0, 16).replace('T', ' ')}</span>
                      </div>
                    </div>
                    <div className="cq-actions">
                      <button
                        className="btn-ghost cq-btn"
                        disabled={busy}
                        title="复制整段对话为新分支"
                        onClick={() => void branch(c.id)}
                      >
                        分支
                      </button>
                      <button
                        className="btn-ghost cq-btn deny"
                        disabled={busy}
                        title="彻底删除：消息、检索索引与分支记录一并清理"
                        onClick={() => void remove(c.id)}
                      >
                        彻底删除
                      </button>
                    </div>
                  </div>
                ))
              )
            ) : (
              <>
                <div className="conv-search-bar">
                  <input
                    className="form-input"
                    value={targetPath}
                    placeholder="回滚目标（工作空间内相对路径，如 deliverables/分析报告.md）"
                    aria-label="回滚目标路径"
                    onChange={(e) => setTargetPath(e.target.value)}
                  />
                </div>
                <div className="conv-hint">
                  快照只覆盖工作副本与交付物；回滚为原子替换，<strong>不会写入案件原件</strong>。
                </div>
                {snapshots.length === 0 ? (
                  <div className="viz-empty">本案件暂无产物快照。</div>
                ) : (
                  snapshots.map((s) => (
                    <div className="cq-row" key={s.snapshotId}>
                      <div className="cq-main">
                        <div className="cq-badges">
                          <span className="rr-role">{s.name}</span>
                          <span className="st-badge st-neutral">{s.artifactKind}</span>
                          <span className="st-badge st-neutral">{(s.bytes / 1024).toFixed(0)} KB</span>
                        </div>
                        <div className="cq-meta">
                          <span className="cq-src none">sha:{s.sha256.slice(0, 12)}</span>
                          <span className="cq-src none">{s.createdAt.slice(0, 16).replace('T', ' ')}</span>
                          {s.note && <span className="cq-src none">{s.note}</span>}
                        </div>
                      </div>
                      <div className="cq-actions">
                        <button className="btn-ghost cq-btn" disabled={busy} onClick={() => void rollback(s)}>
                          回滚到此版本
                        </button>
                      </div>
                    </div>
                  ))
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
