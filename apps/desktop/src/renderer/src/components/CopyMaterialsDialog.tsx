import { useEffect, useRef, useState, type JSX } from 'react'
import type { AgentMode, DocumentMeta } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'

export function CopyMaterialsDialog({ caseId, onClose }: { caseId: string; onClose: () => void }): JSX.Element {
  const [documents, setDocuments] = useState<DocumentMeta[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [mode, setMode] = useState<AgentMode>('contract_review')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const request = useRef(crypto.randomUUID())
  const target = useRef<string | null>(null)
  const card = useDialogA11y(() => { if (!busy) onClose() }, true)
  useEffect(() => {
    let alive = true
    void getBridge()?.document.list(caseId).then((rows) => {
      if (alive) setDocuments(rows.filter((row) => row.status === 'ready' || row.status === 'partial'))
    }).catch(() => { if (alive) setError('材料加载失败') })
    return () => { alive = false }
  }, [caseId])
  const continueTask = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge || !selected.length || useApp.getState().wordEditingDirty) {
      setError('请选择材料，并先保存当前 Word 编辑')
      return
    }
    setBusy(true)
    try {
      if (!target.current) {
        const created = await bridge.case.create({
          title: '历史材料继续任务', caseType: mode === 'contract_review' ? 'contract' : 'other',
          clientName: '', defaultMode: mode, clientRequestId: `create-${request.current}`
        })
        target.current = created.id
      }
      await bridge.case.copyMaterials({
        fromCaseId: caseId, toCaseId: target.current, documentIds: selected,
        clientRequestId: `copy-${request.current}`, confirmed: true
      })
      await useApp.getState().loadCases()
      await useApp.getState().setCurrentCase(target.current)
      onClose()
    } catch { setError('复制未完成，请重试。已创建容器会复用，原件未移动。') }
    finally { setBusy(false) }
  }
  return <div className="modal-overlay" role="dialog" aria-modal="true" aria-label="在新任务继续">
    <div className="modal-card" ref={card} tabIndex={-1}>
      <div className="modal-head"><h3>在新任务继续</h3></div>
      <div className="modal-body">
        <p>仅复制所选材料和解析文本，保留来源关系。原件归属不变，不自动执行 AI。</p>
        <select className="form-input" aria-label="新任务模式" value={mode} disabled={busy || target.current !== null}
          onChange={(event) => setMode(event.target.value as AgentMode)}>
          <option value="contract_review">合同审查</option><option value="document_generation">文书生成</option>
        </select>
        {documents.map((doc) => <label key={doc.id} className="contract-check">
          <input type="checkbox" disabled={busy || target.current !== null} checked={selected.includes(doc.id)}
            onChange={(event) => setSelected((ids) => event.target.checked ? [...ids, doc.id] : ids.filter((id) => id !== doc.id))} />
          {doc.title}
        </label>)}
        {!documents.length && <p>没有已完成解析的可复制材料。</p>}
        {error && <p role="alert" className="contract-warning">{error}</p>}
      </div>
      <div className="modal-foot">
        <button className="btn-ghost" disabled={busy} onClick={onClose}>取消</button>
        <button className="btn-primary" disabled={busy || !selected.length} onClick={() => void continueTask()}>确认复制并进入新任务</button>
      </div>
    </div>
  </div>
}
