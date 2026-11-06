import { useEffect, useState, type JSX } from 'react'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 辩护策略立场选项（本次任务生效，不回写案件设置） */
const STANCES: Array<{ key: string; label: string; desc: string }> = [
  { key: '原告', label: '原告方', desc: '围绕原告主张组织进攻策略' },
  { key: '被告', label: '被告方', desc: '围绕抗辩事由组织防御策略' },
  { key: '中立', label: '中立分析', desc: '双方立场并重，评估各方优劣' }
]

/**
 * 辩护策略立场确认对话框（REQ-STR-001 放宽）：
 * 未在案件设置委托人身份也可发起策略分析；输入未明确立场时在此询问，
 * 选择后随本次任务生效（agent.start.strategyStance），不回写案件设置。
 */
export function StrategyStartDialog(): JSX.Element | null {
  const { strategyStartOpen, setStrategyStartOpen, input, currentCaseId, cases, startStrategyTask } = useApp()

  const [stance, setStance] = useState('')

  // 打开时预选案件已设置的委托人身份（若命中选项），否则默认被告方（策略分析最常见立场）
  const curFileRole = useApp((s) => s.cases.find((c) => c.id === s.currentCaseId)?.clientRole ?? '')
  useEffect(() => {
    if (!strategyStartOpen) return
    const hit = STANCES.find((x) => curFileRole.includes(x.key))
    setStance(hit ? hit.key : '被告')
  }, [strategyStartOpen, curFileRole])

  const close = (): void => setStrategyStartOpen(false)
  const cardRef = useDialogA11y(close, strategyStartOpen)
  if (!strategyStartOpen) return null

  return (
    <div className="modal-overlay" onClick={close}>
      <div ref={cardRef} tabIndex={-1} className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>确认辩护立场</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>
        <div className="modal-body">
          <div className="form-row">
            <span className="form-label">本次策略分析站在哪一方？<em>*</em></span>
            <div className="cr-roles">
              {STANCES.map((s) => (
                <button
                  key={s.key}
                  className={`cr-role wide ${stance === s.key ? 'on' : ''}`}
                  onClick={() => setStance(s.key)}
                >
                  <b>{s.label}</b>
                  <span>{s.desc}</span>
                </button>
              ))}
            </div>
          </div>
          {input.trim() && (
            <div className="cr-empty">确认后将立即执行：{input.trim().slice(0, 60)}{input.trim().length > 60 ? '…' : ''}</div>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn-ghost" onClick={close}>
            取消
          </button>
          <button className="btn-primary" disabled={!stance || !input.trim() || !currentCaseId} onClick={() => startStrategyTask(stance)}>
            开始分析
          </button>
        </div>
      </div>
    </div>
  )
}
