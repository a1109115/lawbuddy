import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { useApp, type NewCaseForm } from '../state/store'
import { Icon } from './Icon'

/** 委托人身份选项按案件类型联动（PRD 8.2：根据案件类型提供可选项） */
const ROLE_OPTIONS: Record<string, string[]> = {
  criminal: ['犯罪嫌疑人', '被告人', '被害人', '自诉人', '附带民事原告'],
  civil: ['原告', '被告', '上诉人', '被上诉人', '申请人', '被申请人'],
  commercial: ['申请人', '被申请人', '甲方', '乙方', '债权人', '债务人'],
  administrative: ['原告', '被告（行政机关）', '申请人', '被申请人'],
  contract: ['甲方', '乙方', '采购方', '供应方', '出租方', '承租方'],
  other: ['委托方', '相对方']
}

const CASE_TYPES: Array<{ value: NewCaseForm['caseType']; label: string }> = [
  { value: 'civil', label: '民事' },
  { value: 'criminal', label: '刑事' },
  { value: 'commercial', label: '商事' },
  { value: 'administrative', label: '行政' },
  { value: 'contract', label: '非诉合同' },
  { value: 'other', label: '其他' }
]

const STAGES = ['', '侦查', '审查起诉', '一审', '二审', '再审', '执行', '仲裁', '谈判']

const MODE_OPTS: Array<{ value: 'yj' | 'ht' | 'ws'; label: string }> = [
  { value: 'yj', label: '法律阅卷' },
  { value: 'ht', label: '合同审查' },
  { value: 'ws', label: '文书生成' }
]

/** 新建案件向导（PRD 8.2 字段；创建失败保留已填内容） */
export function NewCaseDialog(): JSX.Element | null {
  const { creatingCase, setCreatingCase, createCase, mode, cases } = useApp()
  const requestId = useRef(crypto.randomUUID())
  const [form, setForm] = useState<NewCaseForm>({
    title: '',
    caseType: 'civil',
    clientName: '',
    clientRole: '',
    stage: '',
    defaultMode: 'yj',
    linkedCaseId: ''
  })
  const [submitting, setSubmitting] = useState(false)
  useEffect(() => {
    if (creatingCase) {
      requestId.current = crypto.randomUUID()
      setForm({ title: '', caseType: mode === 'ht' ? 'contract' : mode === 'ws' ? 'other' : 'civil',
        clientName: '', clientRole: '', stage: '', defaultMode: mode, linkedCaseId: '' })
    }
  }, [creatingCase, mode])

  const roles = useMemo(() => ROLE_OPTIONS[form.caseType] ?? ROLE_OPTIONS.other, [form.caseType])

  const cardRef = useDialogA11y(() => setCreatingCase(false), creatingCase)

  if (!creatingCase) return null

  const set = <K extends keyof NewCaseForm>(k: K, v: NewCaseForm[K]): void => setForm((f) => ({ ...f, [k]: v }))

  const setType = (t: NewCaseForm['caseType']): void =>
    setForm((f) => ({
      ...f,
      caseType: t,
      // 切换类型后原身份不在新选项中则清空
      clientRole: (ROLE_OPTIONS[t] ?? []).includes(f.clientRole) ? f.clientRole : ''
    }))

  const valid = form.title.trim() !== '' && (form.defaultMode !== 'yj' || form.clientName.trim() !== '')

  const submit = async (draft = form): Promise<void> => {
    if ((draft.defaultMode !== 'ws' && !valid) || submitting) return
    setSubmitting(true)
    const ok = await createCase({ ...draft, clientRequestId: requestId.current })
    setSubmitting(false)
    if (ok) setCreatingCase(false)
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="新建任务"
      onClick={(e) => {
        if (e.target === e.currentTarget) setCreatingCase(false)
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card">
        <div className="modal-head">
          <h3>新建任务</h3>
          <button className="icon-btn" aria-label="关闭" onClick={() => setCreatingCase(false)}>
            <Icon id="i-x" />
          </button>
        </div>

        <div className="modal-body">
          <div className="form-row">
            <span className="form-label">任务模式</span>
            <div className="mode-radio" role="radiogroup" aria-label="任务模式">
              {MODE_OPTS.map((m) => (
                <button key={m.value} type="button" role="radio"
                  disabled={submitting} aria-checked={form.defaultMode === m.value}
                  className={`mode-radio-item${form.defaultMode === m.value ? ' on' : ''}`}
                  onClick={() => {
                    setForm((f) => ({
                      ...f,
                      defaultMode: m.value,
                      caseType: m.value === 'ht' ? 'contract' : m.value === 'ws' ? 'other' : 'civil',
                      clientRole: '',
                      stage: '',
                      linkedCaseId: ''
                    }))
                  }}>{m.label}</button>
              ))}
            </div>
            <small>创建后模式固定。文书生成可直接关联已有案件，共享其材料与案件记忆。</small>
          </div>

          {form.defaultMode === 'ws' && (
            <div className="form-row">
              <label className="form-label" htmlFor="f-linked-case">
                关联案件（选填）
              </label>
              <select
                id="f-linked-case"
                className="form-input"
                value={form.linkedCaseId ?? ''}
                disabled={submitting}
                onChange={(e) => set('linkedCaseId', e.target.value)}
              >
                <option value="">不关联（仅使用本任务新导入的材料）</option>
                {cases.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
              </select>
              <small>
                关联后可直接读取该案件的材料与案件记忆用于文书生成；共享为只读，材料、
                记忆与原件归属不变。
              </small>
            </div>
          )}

          {form.defaultMode !== 'ws' && <>
          <div className="form-row">
            <label className="form-label" htmlFor="f-title">
              任务名称 <em>*</em>
            </label>
            <input
              id="f-title"
              className="form-input"
              value={form.title}
              placeholder="例如：张三诉李四民间借贷纠纷案"
              onChange={(e) => set('title', e.target.value)}
            />
          </div>

          <div className="form-grid">
            {form.defaultMode === 'yj' &&
            <div className="form-row">
              <label className="form-label" htmlFor="f-type">
                案件类型 <em>*</em>
              </label>
              <select id="f-type" className="form-input" value={form.caseType} onChange={(e) => setType(e.target.value as NewCaseForm['caseType'])}>
                {CASE_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>}
            <div className="form-row">
              <label className="form-label" htmlFor="f-client">
                {form.defaultMode === 'yj' ? <>委托人或客户 <em>*</em></> : '客户 / 我方代称（选填）'}
              </label>
              <input
                id="f-client"
                className="form-input"
                value={form.clientName}
                placeholder="可先使用代称"
                onChange={(e) => set('clientName', e.target.value)}
              />
            </div>
          </div>

          {form.defaultMode === 'yj' ? <div className="form-grid">
            <div className="form-row">
              <label className="form-label" htmlFor="f-role">
                委托人身份
              </label>
              <select id="f-role" className="form-input" value={form.clientRole} onChange={(e) => set('clientRole', e.target.value)}>
                <option value="">稍后设置（策略任务前必填）</option>
                {roles.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-row">
              <label className="form-label" htmlFor="f-stage">
                程序阶段
              </label>
              <select id="f-stage" className="form-input" value={form.stage} onChange={(e) => set('stage', e.target.value)}>
                {STAGES.map((s) => (
                  <option key={s} value={s}>
                    {s || '未选择'}
                  </option>
                ))}
              </select>
            </div>
          </div> : <>
            <div className="form-row">
              <label className="form-label" htmlFor="f-contract-type">合同类型（选填）</label>
              <input id="f-contract-type" className="form-input" placeholder="待识别"
                value={form.contractType ?? ''} onChange={(e) => set('contractType', e.target.value)} />
            </div>
            <div className="form-row">
              <label className="form-label" htmlFor="f-background">审查背景（选填）</label>
              <textarea id="f-background" className="form-input" value={form.reviewBackground ?? ''}
                onChange={(e) => set('reviewBackground', e.target.value)} />
            </div>
          </>}
          </>}
        </div>

        <div className="modal-foot">
          <button className="btn-ghost" onClick={() => setCreatingCase(false)}>
            取消
          </button>
          <button className="btn-primary" disabled={(form.defaultMode !== 'ws' && !valid) || submitting} onClick={() => void submit()}>
            {submitting ? '创建中…' : form.defaultMode === 'ws' ? '进入文书生成' : '创建任务'}
          </button>
        </div>
      </div>
    </div>
  )
}
