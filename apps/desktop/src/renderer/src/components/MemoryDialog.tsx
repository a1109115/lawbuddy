import { useEffect, useState, type JSX } from 'react'
import type { MemoryEntry } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { useApp } from '../state/store'
import { Icon } from './Icon'

const KIND_LABEL: Record<string, string> = {
  person: '人物',
  timeline: '时间线',
  evidence: '证据',
  fact: '事实',
  dispute: '争点',
  correction: '更正',
  note: '备注',
  observation: '观察'
}

const STATUS_LABEL: Record<string, { label: string; cls: string; hint: string }> = {
  active: { label: '生效', cls: 'st-favorable', hint: '参与召回与上下文装配' },
  stale: { label: '已失效', cls: 'st-unfavorable', hint: '来源文档已删除或替换，重新计算前不再召回为有效事实' },
  superseded: { label: '已被更正', cls: 'st-neutral', hint: '原值保留供追溯（M-004）' }
}

/**
 * 案件记忆（DEV-AGT-007 / M-001~M-006）：
 * - 案件记忆随案件保存并强制案件隔离，来源可回溯（文档与页码）；
 * - 用户更正：新值生效、原值保留（superseded，可在列表中追溯）；
 * - 删除：SQLite 与召回索引同时清理，不再召回；
 * - 会话工作记忆随任务结束自动清理，不在此列；用户偏好在「审计与设置」范围外暂不展示。
 */
export function MemoryDialog(): JSX.Element | null {
  const { memoryOpen, setMemoryOpen, memories, memoryLoading, loadMemories, correctMemory, forgetMemory, currentCaseId, openCitation, documents, toast } =
    useApp()
  const [tab, setTab] = useState<'active' | 'history'>('active')
  const [editing, setEditing] = useState<Record<string, string>>({})

  useEffect(() => {
    if (memoryOpen && currentCaseId) void loadMemories()
  }, [memoryOpen, currentCaseId, loadMemories])

  const close = (): void => {
    setEditing({})
    setMemoryOpen(false)
  }
  const cardRef = useDialogA11y(close, memoryOpen)

  if (!memoryOpen) return null

  const docNames: Record<string, string> = {}
  for (const d of documents) docNames[d.id] = d.name

  const actives = memories.filter((m) => m.status === 'active')
  const history = memories.filter((m) => m.status === 'stale' || m.status === 'superseded')
  const list = tab === 'active' ? actives : history

  const submit = async (m: MemoryEntry): Promise<void> => {
    const value = (editing[m.id] ?? '').trim()
    if (!value) {
      toast('更正内容不能为空')
      return
    }
    await correctMemory(m.id, value)
    setEditing((s) => {
      const next = { ...s }
      delete next[m.id]
      return next
    })
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="案件记忆"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card claims-card">
        <div className="modal-head">
          <h3>案件记忆</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>

        <div className="modal-body">
          <div className="viz-bar">
            <div className="viz-tabs" role="tablist" aria-label="记忆分组">
              <button
                role="tab"
                aria-selected={tab === 'active'}
                className={`viz-tab${tab === 'active' ? ' on' : ''}`}
                onClick={() => setTab('active')}
              >
                <Icon id="i-bulb" />
                生效（{actives.length}）
              </button>
              <button
                role="tab"
                aria-selected={tab === 'history'}
                className={`viz-tab${tab === 'history' ? ' on' : ''}`}
                onClick={() => setTab('history')}
              >
                <Icon id="i-clock" />
                失效与更正（{history.length}）
              </button>
            </div>
            <button className="viz-toggle" title="刷新" onClick={() => void loadMemories()}>
              <Icon id="i-new" />
              刷新
            </button>
          </div>

          {memoryLoading ? (
            <div className="viz-empty">加载中…</div>
          ) : list.length === 0 ? (
            <div className="viz-empty">
              {tab === 'active'
                ? '当前任务及关联案件暂无记忆。完成一次有引用来源的分析后，人物与时间线会自动入库。'
                : '暂无失效或被更正的记忆。'}
            </div>
          ) : (
            list.map((m) => {
              const st = STATUS_LABEL[m.status] ?? { label: m.status, cls: 'st-neutral', hint: '' }
              // 关联案件（文书生成只读共享）的记忆：可见、可跳转来源，但不在此任务内更正/删除
              const shared = !!m.caseId && !!currentCaseId && m.caseId !== currentCaseId
              return (
                <div className="cq-row" key={m.id} aria-label="记忆条目">
                  <div className="cq-main">
                    <div className="cq-badges">
                      <span className="rr-role">{KIND_LABEL[m.kind] ?? m.kind}</span>
                      <span className={`st-badge ${st.cls}`} title={st.hint}>
                        {st.label}
                      </span>
                      {shared && (
                        <span className="st-badge st-neutral" title="来自关联案件（只读共享），更正与删除请到来源案件操作">
                          关联案件
                        </span>
                      )}
                      <span className="st-badge st-neutral">{m.key}</span>
                    </div>
                    <div className="cq-text">{m.value}</div>
                    <div className="cq-meta">
                      {m.source ? (
                        <button
                          className="cq-src mono clickable"
                          title={`${docNames[m.source.documentId] ?? m.source.documentId} · 第${m.source.pageNo ?? '?'}页（点击跳转原文）`}
                          onClick={() => {
                            close()
                            openCitation(m.source!.documentId, Number(m.source!.pageNo) || 1)
                          }}
                        >
                          <Icon id="i-doc" />
                          {docNames[m.source.documentId] ?? `${m.source.documentId.slice(0, 8)}…`}
                          {m.source.pageNo ? ` · 第${m.source.pageNo}页` : ''}
                        </button>
                      ) : (
                        <span className="cq-src none">无来源</span>
                      )}
                      <span className="cq-time">{(m.updatedAt || '').slice(0, 16).replace('T', ' ')}</span>
                    </div>
                  </div>
                  {m.status === 'active' && !shared && (
                    <div className="cq-actions">
                      <input
                        className="form-input cq-note"
                        placeholder="更正后的内容（原值保留可追溯）"
                        value={editing[m.id] ?? ''}
                        onChange={(e) => setEditing({ ...editing, [m.id]: e.target.value })}
                      />
                      <button className="btn-primary cq-btn" title="更正：新值生效，原值保留" onClick={() => void submit(m)}>
                        更正
                      </button>
                      <button
                        className="btn-ghost cq-btn deny"
                        title="删除：不再参与召回与上下文装配"
                        onClick={() => void forgetMemory(m.id)}
                      >
                        删除
                      </button>
                    </div>
                  )}
                </div>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}