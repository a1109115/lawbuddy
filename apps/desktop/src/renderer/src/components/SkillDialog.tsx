import { useEffect, useState, type JSX } from 'react'
import type { SkillInfo } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { skillDisplay } from '../lib/capabilityMeta'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/**
 * 技能管理（REQ-AI-003 / REQ-AI-007）：已安装技能的查看与删除。
 * 删除走 Core skill.remove：写审计；workspace:// 来源的技能会一并删除
 * 工作空间技能文件夹（含 SKILL.md 与配套资源），文件级来源在删除前明确提示。
 */
export function SkillDialog(): JSX.Element | null {
  const { skillDialogOpen, setSkillDialogOpen, loadCapabilities, toast } = useApp()

  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [selId, setSelId] = useState<string | null>(null)
  // 待删除确认的技能（非空时显示确认对话框，防误删）
  const [delTarget, setDelTarget] = useState<SkillInfo | null>(null)
  const [busy, setBusy] = useState(false)

  const bridge = getBridge()

  const reload = async (): Promise<void> => {
    if (!bridge) return
    try {
      setSkills(await bridge.skill.list())
    } catch (e) {
      toast(`技能列表加载失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useEffect(() => {
    if (skillDialogOpen) void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 打开对话框时刷新一次即可
  }, [skillDialogOpen])

  const sel = skills.find((s) => s.id === selId) ?? null

  // Esc 分层：先关删除确认，再关对话框（App 全局 Esc 不处理本弹窗，由这里接管）
  const close = (): void => {
    if (delTarget) {
      setDelTarget(null)
      return
    }
    setSkillDialogOpen(false)
  }
  const delClose = (): void => setDelTarget(null)
  const cardRef = useDialogA11y(close, skillDialogOpen)
  const delCardRef = useDialogA11y(delClose, delTarget !== null)
  if (!skillDialogOpen) return null

  const removeSkill = async (): Promise<void> => {
    if (!bridge || !delTarget) return
    const target = delTarget
    setBusy(true)
    try {
      await bridge.skill.remove({ skillId: target.id })
      setDelTarget(null)
      if (selId === target.id) setSelId(null)
      await Promise.all([reload(), loadCapabilities()]) // 同步刷新 / 菜单的全局技能清单
      toast('技能已删除')
    } catch (e) {
      toast(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const isWorkspace = (s: SkillInfo): boolean => (s.sourceUrl || '').startsWith('workspace://')

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="技能库"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card skill-card">
        <div className="modal-head">
          <h3>技能库</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>

        <div className="skill-layout">
          {/* 左列：已安装技能列表 */}
          <div className="skill-list">
            {skills.length === 0 && (
              <div className="skill-empty">
                尚无已安装技能；把技能文件或目录发送给助手即可安装，或把技能文件放入工作空间 skills 目录自动同步。
              </div>
            )}
            {skills.map((s) => {
              const d = skillDisplay(s)
              return (
                <button key={s.id} className={`skill-item${s.id === selId ? ' on' : ''}`} onClick={() => setSelId(s.id)}>
                  <span className="skill-item-name">{d.title}</span>
                  {d.desc && <span className="skill-item-desc">{d.desc}</span>}
                  <span className="skill-item-meta">
                    {!s.enabled ? '已禁用 · ' : ''}
                    {isWorkspace(s) ? '工作空间' : '已安装'}
                  </span>
                </button>
              )
            })}
          </div>

          {/* 右列：详情与删除 */}
          <div className="skill-detail">
            {!sel ? (
              <div className="skill-empty">选择左侧技能查看详情。</div>
            ) : (
              <>
                <div className="skill-detail-head">
                  <div>
                    <div className="skill-detail-name">{skillDisplay(sel).title}</div>
                    <div className="skill-detail-source">{sel.sourceUrl || '本地技能'}</div>
                    {sel.description && <div className="skill-desc">{sel.description}</div>}
                  </div>
                  <div className="skill-detail-actions">
                    <button className="btn-ghost btn-mini danger" onClick={() => setDelTarget(sel)}>
                      删除
                    </button>
                  </div>
                </div>

                <div className="skill-section">
                  <span className="form-label">允许工具（{sel.allowedTools.length}）</span>
                  {sel.allowedTools.length === 0 ? (
                    <p className="form-hint">未限制工具白名单。</p>
                  ) : (
                    <div className="skill-tools">
                      {sel.allowedTools.map((t) => (
                        <span key={t} className="term-chip">
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <div className="skill-section">
                  <span className="form-label">技能提示词（SKILL.md）</span>
                  <pre className="skill-out skill-prompt">{sel.promptMd || '（空）'}</pre>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* 删除确认（防误删；工作空间技能会连同本地文件一起删除） */}
      {delTarget && (
        <div
          className="modal-overlay"
          role="alertdialog"
          aria-modal="true"
          aria-label="删除技能"
          onClick={(e) => {
            if (e.target === e.currentTarget) delClose()
          }}
        >
          <div ref={delCardRef} tabIndex={-1} className="modal-card sess-del-card">
            <div className="modal-head">
              <h3>删除技能</h3>
              <button className="icon-btn" aria-label="关闭" onClick={delClose}>
                <Icon id="i-x" />
              </button>
            </div>
            <div className="modal-body">
              <p className="del-warning">
                <Icon id="i-alert" />
                <span>
                  将删除技能「{skillDisplay(delTarget).title}」，
                  {isWorkspace(delTarget) && '其工作空间技能文件夹（含 SKILL.md 与配套资源）会一并删除，'}
                  <strong>此操作不可恢复</strong>。
                </span>
              </p>
            </div>
            <div className="modal-foot">
              <button className="btn-ghost" onClick={delClose}>
                取消
              </button>
              <button className="btn-danger" disabled={busy} onClick={() => void removeSkill()}>
                删除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
