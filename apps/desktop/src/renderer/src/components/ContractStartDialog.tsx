import { useEffect, useState, type JSX } from 'react'
import type { ContractStartInput } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 审查方角色（PRD 14.2；与 Core REVIEWER_ROLES 对应） */
const ROLES: Array<{ key: string; label: string }> = [
  { key: 'seller', label: '卖方 / 乙方' },
  { key: 'buyer', label: '买方 / 甲方' },
  { key: 'lessor', label: '出租人' },
  { key: 'lessee', label: '承租人' },
  { key: 'lender', label: '出借人' },
  { key: 'borrower', label: '借款人' },
  { key: 'employer', label: '发包方 / 雇主' },
  { key: 'contractor', label: '承包方 / 受雇方' },
  { key: 'custom', label: '自定义…' }
]

const APPETITES: Array<{ key: string; label: string; desc: string }> = [
  { key: 'conservative', label: '保守', desc: '从严把关，宁可多提示风险' },
  { key: 'balanced', label: '均衡', desc: '常规商业尺度（推荐）' },
  { key: 'aggressive', label: '进取', desc: '仅提示高风险项，减少商务摩擦' }
]

/**
 * 合同审查发起对话框（PRD 14.2 输入表）：
 * 选择主合同 / 审查方角色 / 风险偏好，经 agent.start.contractInput 通道发起。
 */
export function ContractStartDialog(): JSX.Element | null {
  const { contractStartOpen, setContractStartOpen, documents, input, startContractTask } = useApp()

  const [docId, setDocId] = useState('')
  const [role, setRole] = useState('buyer')
  const [customRole, setCustomRole] = useState('')
  const [appetite, setAppetite] = useState('balanced')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // 可解析文档变化时回落选中（优先已选中的阅读文档）
  const curFileId = useApp((s) => s.curFileId)
  useEffect(() => {
    if (!contractStartOpen) return
    setError('')
    const ids = documents.map((d) => d.id)
    setDocId(curFileId && ids.includes(curFileId) ? curFileId : (ids[0] ?? ''))
  }, [contractStartOpen, documents, curFileId])

  const close = (): void => setContractStartOpen(false)
  const cardRef = useDialogA11y(close, contractStartOpen)
  if (!contractStartOpen) return null

  const submit = async (): Promise<void> => {
    setError('')
    if (!docId) {
      setError('请选择主合同文档（先在案件中导入）')
      return
    }
    if (!role) {
      setError('请选择审查方角色')
      return
    }
    if (role === 'custom' && !customRole.trim()) {
      setError('自定义审查方必须填写具体角色')
      return
    }
    const inp: ContractStartInput = {
      primaryDocumentId: docId,
      reviewerRole: role,
      customRole: role === 'custom' ? customRole.trim() : null,
      riskAppetite: appetite
    }
    setBusy(true)
    await startContractTask(input.trim() || '合同风险审查', inp)
    setBusy(false)
  }

  return (
    <div className="modal-overlay" onClick={close}>
      <div ref={cardRef} tabIndex={-1} className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>发起合同审查</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>
        <div className="modal-body">
          <div className="form-row">
            <span className="form-label">主合同<em>*</em></span>
            {documents.length === 0 ? (
              <div className="cr-empty">当前案件还没有文档，请先导入合同文件</div>
            ) : (
              <select className="form-input" value={docId} onChange={(e) => setDocId(e.target.value)}>
                {documents.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="form-row">
            <span className="form-label">审查方立场（我方视角）<em>*</em></span>
            <div className="cr-roles">
              {ROLES.map((r) => (
                <button
                  key={r.key}
                  className={`cr-role ${role === r.key ? 'on' : ''}`}
                  onClick={() => setRole(r.key)}
                >
                  {r.label}
                </button>
              ))}
            </div>
            {role === 'custom' && (
              <input
                className="form-input"
                placeholder="填写具体角色，如：股东方 / 债权人"
                value={customRole}
                onChange={(e) => setCustomRole(e.target.value)}
              />
            )}
          </div>

          <div className="form-row">
            <span className="form-label">风险偏好</span>
            <div className="cr-appetites">
              {APPETITES.map((a) => (
                <button
                  key={a.key}
                  className={`cr-role wide ${appetite === a.key ? 'on' : ''}`}
                  onClick={() => setAppetite(a.key)}
                >
                  <b>{a.label}</b>
                  <span>{a.desc}</span>
                </button>
              ))}
            </div>
          </div>

          {error && <div className="cr-error">{error}</div>}
        </div>
        <div className="modal-foot">
          <button className="btn-ghost" onClick={close}>
            取消
          </button>
          <button className="btn-primary" disabled={busy} onClick={() => void submit()}>
            {busy ? '发起中…' : '开始审查'}
          </button>
        </div>
      </div>
    </div>
  )
}
