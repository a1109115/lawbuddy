import { useEffect, useState, type JSX } from 'react'
import { createPortal } from 'react-dom'
import logoMark from '../assets/lawbuddy-avatar.png'
import { CASE_TYPE_LABEL } from '../data/mock'
import { useDialogA11y, useEscapeKey } from '../hooks/useDialogA11y'
import { TERMINAL, useApp, type ChatSession } from '../state/store'
import { Icon } from './Icon'
import { CopyMaterialsDialog } from './CopyMaterialsDialog'

/** 会话时间显示：今天/昨天显示时分，其余显示月/日 时分 */
const fmtTime = (t: number): string =>
  new Date(t).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

/** 会话内是否存在未终态任务（后台继续执行中） */
const sessionRunning = (s: ChatSession): boolean =>
  s.messages.some((m) => m.role === 'task' && !TERMINAL.includes(m.lifecycle))

/** 案件会话列表：该案件下的全部对话，最近在前；执行中的会话显示徽标 */
function CaseSessions({ caseId, onDelete }: { caseId: string; onDelete: (s: ChatSession) => void }): JSX.Element {
  const chatSessions = useApp((s) => s.chatSessions)
  const activeSessionId = useApp((s) => s.activeSessionId)
  const openSession = useApp((s) => s.openSession)
  const sessions = chatSessions
    .filter((h) => h.caseId === caseId)
    .sort((a, b) => b.updatedAt - a.updatedAt)
  return (
    <div className="hist-panel case-sessions-panel" aria-label="案件会话">
      {sessions.length === 0 ? (
        <div className="archived-empty">暂无会话</div>
      ) : (
        sessions.map((h) => {
          const running = sessionRunning(h)
          return (
            <div key={h.id} className={`hist-item${h.id === activeSessionId ? ' on' : ''}`}>
              <button className="hist-open" title={h.title} onClick={() => void openSession(h.id)}>
                <span className="hist-title">{h.title}</span>
                {running ? (
                  <span className="hist-running">
                    <span className="hist-running-dot" aria-hidden="true" />
                    执行中
                  </span>
                ) : (
                  <span className="hist-time">{fmtTime(h.updatedAt)}</span>
                )}
              </button>
              <button className="archived-del" aria-label="删除该会话" title="删除该会话" onClick={() => onDelete(h)}>
                <Icon id="i-x" />
              </button>
            </div>
          )
        })
      )}
    </div>
  )
}

/** 左侧栏（PRD 9.3）：品牌 + 新建案件 + 当前/最近案件（会话归案件下）+ 归档 + 导航 + 底部 */
export function Sidebar(): JSX.Element {
  const {
    leftW,
    leftHide,
    cases,
    archivedCases,
    currentCaseId,
    setCurrentCase,
    archiveCase,
    restoreCase,
    setDeletingCase,
    deleteSession,
    casePanelOpen,
    setCasePanelOpen,
    setCreatingCase,
    setClaimsQueueOpen,
    setAuditOpen,
    setSkillDialogOpen,
    setMemoryOpen
  } = useApp()
  const claimsQueueCount = useApp((s) => s.claimsQueueCount)
  const [recentsOpen, setRecentsOpen] = useState(false)
  // 案件会话折叠态：点击案件卡片切换（默认展开）
  const [sessOpen, setSessOpen] = useState(true)
  // 待删除确认的会话（非空时显示确认对话框，防误删）
  const [delTarget, setDelTarget] = useState<ChatSession | null>(null)
  const [menuId, setMenuId] = useState<string | null>(null)
  const [copyCaseId, setCopyCaseId] = useState<string | null>(null)
  const closeDelDialog = (): void => setDelTarget(null)
  const delCardRef = useDialogA11y(closeDelDialog, delTarget !== null)

  // 点击菜单外关闭案件操作菜单（菜单本身也可能渲染在 .case-menu-wrap 之外）
  useEffect(() => {
    if (!menuId) return
    const onDoc = (e: MouseEvent): void => {
      if (!(e.target as HTMLElement).closest('.case-menu-wrap, .case-menu')) setMenuId(null)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [menuId])
  // Esc 关闭案件操作菜单（设计规范 §7：Esc 先关闭浮层）
  useEscapeKey(() => setMenuId(null), menuId !== null)

  const current = cases.find((c) => c.id === currentCaseId) ?? null
  const recents = cases.filter((c) => c.id !== currentCaseId).slice(0, 5)
  // 最近案件相对时间：今天/昨天/N天前，超过一周回退日期
  const relTime = (iso?: string): string => {
    if (!iso) return ''
    const t = new Date(iso).getTime()
    if (Number.isNaN(t)) return iso.slice(0, 10)
    const now = new Date()
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
    const hm = new Date(t).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    if (t >= startToday) return `今天 ${hm}`
    if (t >= startToday - 86400000) return `昨天 ${hm}`
    const days = Math.floor((startToday - t) / 86400000)
    if (days < 7) return `${days} 天前`
    return new Date(t).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
  }

  return (
    <aside className={`sidebar${leftHide ? ' hide' : ''}`} style={leftHide ? undefined : { width: leftW }}>
      {/* 侧栏本身是 z-index 5 的层叠上下文，弹窗留在侧栏内会被分隔条/标题栏压在下层，
          统一通过 Portal 挂到 body 上 */}
      {copyCaseId &&
        createPortal(<CopyMaterialsDialog caseId={copyCaseId} onClose={() => setCopyCaseId(null)} />, document.body)}
      <div className="brand">
        <div className="appicon">
          <img className="logo-img" src={logoMark} alt="LawBuddy" />
        </div>
        <div>
          <div className="brand-name">LawBuddy</div>
          <div className="brand-slogan">AI让法律更简单</div>
        </div>
      </div>
      <button className="btn-newcase" onClick={() => setCreatingCase(true)}>
        <Icon id="i-plus" />
        新建任务
      </button>

      {/* 当前案件与其会话列表 */}
      <div className="case-section" aria-label="任务列表">
        {current ? (
          <div
            className="case-card on clickable"
            title={current.title}
            aria-expanded={sessOpen}
            onClick={() => setSessOpen(!sessOpen)}
          >
            <div className="case-card-main">
              <span className="case-name">{current.title}</span>
            </div>
            <div className="case-meta">
              <span className="case-tag">{CASE_TYPE_LABEL[current.caseType] ?? current.caseType}</span>
              <span className="case-time">{(current.updatedAt || '').slice(0, 10)}</span>
              <Icon id="i-caret" className={`case-caret${sessOpen ? ' open' : ''}`} />
              <div className="case-menu-wrap">
                <button
                  className="case-more"
                  aria-label="案件操作"
                  title="案件操作"
                  aria-expanded={menuId === current.id}
                  onClick={(e) => {
                    e.stopPropagation() // 菜单按钮不触发会话折叠
                    setMenuId(menuId === current.id ? null : current.id)
                  }}
                >
                  <Icon id="i-dots" />
                </button>
              </div>
            </div>
            {/* 当前任务操作菜单：在卡片内展开，避免浮层遮挡下方的最近任务 */}
            {menuId === current.id && (
              <div className="case-menu case-menu-inline" role="menu" aria-label="案件操作">
                <button role="menuitem" onClick={(event) => {
                  event.stopPropagation()
                  setMenuId(null)
                  setCopyCaseId(current.id)
                }}>在新任务继续…</button>
                <button
                  role="menuitem"
                  onClick={(event) => {
                    event.stopPropagation()
                    setMenuId(null)
                    void archiveCase(current.id)
                  }}
                >
                  归档任务
                </button>
                <button
                  role="menuitem"
                  className="danger"
                  onClick={(event) => {
                    event.stopPropagation()
                    setMenuId(null)
                    setDeletingCase(current)
                  }}
                >
                  永久删除…
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="case-empty">暂无任务，点击「新建任务」开始</div>
        )}

        {/* 会话直接归属案件：点击案件卡片折叠/展开该案件的会话列表 */}
        {current && (
          <div className={`sess-collapse${sessOpen ? ' open' : ''}`}>
            <div className="sess-clip">
              <CaseSessions caseId={current.id} onDelete={setDelTarget} />
            </div>
          </div>
        )}

        {recents.length > 0 && (
          <div className={`recents${recentsOpen ? ' open' : ''}`}>
            <button
              className="case-sec-toggle"
              aria-expanded={recentsOpen}
              aria-controls="recents-panel"
              title={recentsOpen ? '收起最近任务' : '展开最近任务'}
              onClick={() => setRecentsOpen(!recentsOpen)}
            >
              <Icon id="i-clock" className="toggle-ic" />
              最近任务
              <span className="side-count">{recents.length}</span>
              <Icon id="i-caret" className="toggle-caret" />
            </button>
            <div className="recents-panel" id="recents-panel" aria-label="最近任务列表" aria-hidden={!recentsOpen}>
              <div className="recents-clip">
                <div className="recents-list">
                  {recents.map((c) => (
                    <div key={c.id} className={`case-row${menuId === c.id ? ' menu-open' : ''}`}>
                      <div className="case-row-line">
                        <span
                          className={`case-hue t-${c.caseType}`}
                          aria-hidden="true"
                          title={CASE_TYPE_LABEL[c.caseType] ?? c.caseType}
                        />
                        <button className="case-row-open" title={c.title} onClick={() => void setCurrentCase(c.id)}>
                          <span className="case-name">{c.title}</span>
                          <span className="case-row-meta">
                            <span className="case-tag">{CASE_TYPE_LABEL[c.caseType] ?? c.caseType}</span>
                            <span className="case-row-time">{relTime(c.updatedAt)}</span>
                          </span>
                        </button>
                        <div className="case-menu-wrap">
                          <button
                            className="case-more"
                            aria-label="案件操作"
                            title="案件操作"
                            aria-expanded={menuId === c.id}
                            onClick={() => setMenuId(menuId === c.id ? null : c.id)}
                          >
                            <Icon id="i-dots" />
                          </button>
                        </div>
                      </div>
                      {/* 菜单在行内展开：侧栏的折叠容器与滚动容器会裁切浮层，展开式与当前任务卡片保持一致 */}
                      {menuId === c.id && (
                        <div className="case-menu case-menu-inline" role="menu" aria-label="案件操作">
                          <button
                            role="menuitem"
                            onClick={() => {
                              setMenuId(null)
                              void setCurrentCase(c.id) // 切换后该案件会话即展示在案件卡下方
                            }}
                          >
                            会话记录
                          </button>
                          <button
                            role="menuitem"
                            onClick={() => {
                              setMenuId(null)
                              void archiveCase(c.id)
                            }}
                          >
                            归档案件
                          </button>
                          <button
                            role="menuitem"
                            className="danger"
                            onClick={() => {
                              setMenuId(null)
                              setDeletingCase(c)
                            }}
                          >
                            永久删除…
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      <nav className="side-nav" aria-label="主导航">
        <button className={`side-item${casePanelOpen ? ' active' : ''}`} onClick={() => setCasePanelOpen(!casePanelOpen)}>
          <Icon id="i-clock" />
          归档记录
          {archivedCases.length > 0 && <span className="side-count">{archivedCases.length}</span>}
        </button>
        {casePanelOpen && (
          <div className="archived-panel" aria-label="归档案件">
            {archivedCases.length === 0 ? (
              <div className="archived-empty">暂无归档案件</div>
            ) : (
              archivedCases.map((c) => (
                <div key={c.id} className="archived-item">
                  <span className="case-name" title={c.title}>
                    {c.title}
                  </span>
                  <button
                    className="archived-del"
                    title="永久删除"
                    aria-label={`永久删除 ${c.title}`}
                    onClick={() => setDeletingCase(c)}
                  >
                    <Icon id="i-x" />
                  </button>
                  <button className="archived-restore" title="恢复案件" aria-label={`恢复案件 ${c.title}`} onClick={() => void restoreCase(c.id)}>
                    <Icon id="i-new" />
                    恢复
                  </button>
                </div>
              ))
            )}
          </div>
        )}
      </nav>

      <div className="side-bottom">
          <button
            className="side-item"
            onClick={() => setClaimsQueueOpen(true)}
            title="待核实、已失效与矛盾证据（PRD 11.4）"
          >
            <Icon id="i-alert" />
            待核实队列
            {claimsQueueCount > 0 && <span className="side-count">{claimsQueueCount}</span>}
          </button>
          <button
            className="side-item"
            onClick={() => setMemoryOpen(true)}
            title="案件记忆：来源可回溯、可更正与删除（DEV-AGT-007）"
          >
            <Icon id="i-bulb" />
            案件记忆
          </button>
          <button
            className="side-item"
            onClick={() => setSkillDialogOpen(true)}
            title="已安装技能管理：查看详情与删除（REQ-AI-003）"
          >
            <Icon id="i-bulb" />
            技能库
          </button>
          <button className="side-item" onClick={() => setAuditOpen(true)} title="审计与诊断">
            <Icon id="i-gear" />
            审计与设置
          </button>
        </div>

      {/* 删除会话确认（防误删；含执行中任务时一并提示将停止任务）
          同样走 Portal，避免被分隔条（z-index 20）和标题栏（z-index 60）盖在弹窗上面 */}
      {delTarget &&
        createPortal(
          <div
            className="modal-overlay"
            role="alertdialog"
            aria-modal="true"
            aria-label="删除会话"
            onClick={(e) => {
              if (e.target === e.currentTarget) closeDelDialog()
            }}
          >
            <div ref={delCardRef} tabIndex={-1} className="modal-card sess-del-card">
              <div className="modal-head">
                <h3>删除会话</h3>
                <button className="icon-btn" aria-label="关闭" onClick={closeDelDialog}>
                  <Icon id="i-x" />
                </button>
              </div>
              <div className="modal-body">
                <p className="del-warning">
                  <Icon id="i-alert" />
                  <span>
                    将删除会话「{delTarget.title}」的本地聊天记录，<strong>此操作不可恢复</strong>。
                    {sessionRunning(delTarget) ? '会话中未完成的任务将被停止。' : ''}
                  </span>
                </p>
              </div>
              <div className="modal-foot">
                <button className="btn-ghost" onClick={closeDelDialog}>
                  取消
                </button>
                <button
                  className="btn-danger"
                  onClick={() => {
                    const id = delTarget.id
                    closeDelDialog()
                    deleteSession(id)
                  }}
                >
                  删除
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </aside>
  )
}
