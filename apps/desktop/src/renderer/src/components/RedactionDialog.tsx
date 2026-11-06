import { useEffect, useState, type JSX } from 'react'
import type { RedactionEntity, RedactionPolicy, RedactionTerm } from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { getBridge } from '../services/bridge'
import { ENTITY_TYPES, POLICY_LABEL, useApp } from '../state/store'
import { Icon } from './Icon'

/** 档位说明（技术方案 11.3；仅个人敏感信息，机构名/案号/信用代码不脱敏） */
const POLICY_DESC: Record<string, string> = {
  light: '仅五类直接标识符（证件号、银行卡、手机号、邮箱、车牌）',
  standard: '直接标识符 + 姓名（推荐；机构名、案号不脱敏）',
  strict: '全部个人敏感类型，含地址与自定义敏感词',
  custom: '自选需要替换的敏感字段类型'
}

/**
 * 案件脱敏与隐私设置（REQ-PRIV-004）：
 * 档位切换（custom 可勾选类型）/ 待人工确认实体 / 自定义敏感词。
 * 待确认实体非空时外发将被阻断（REQ-PRIV-006），在此处逐项确认或驳回。
 */
export function RedactionDialog(): JSX.Element | null {
  const { redactionDialogOpen, setRedactionDialogOpen, cases, currentCaseId, setCaseRedactionPolicy, toast } = useApp()
  const cur = cases.find((c) => c.id === currentCaseId) ?? null

  const [policy, setPolicy] = useState<RedactionPolicy>('standard')
  const [customTypes, setCustomTypes] = useState<string[]>([])
  const [dirty, setDirty] = useState(false)
  const [entities, setEntities] = useState<RedactionEntity[]>([])
  const [terms, setTerms] = useState<RedactionTerm[]>([])
  const [termInput, setTermInput] = useState('')
  const [saving, setSaving] = useState(false)

  const reload = async (caseId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const [ents, tms] = await Promise.all([
        bridge.redaction.listEntities({ caseId, status: 'needs_human' }),
        bridge.redaction.listTerms({ caseId })
      ])
      setEntities(ents)
      setTerms(tms)
    } catch (e) {
      toast(`脱敏数据加载失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  // 打开 / 切换案件时同步档位与列表
  useEffect(() => {
    if (!redactionDialogOpen || !cur) return
    setPolicy(cur.redactionPolicy)
    setCustomTypes(cur.redactionCustomTypes ?? [])
    setDirty(false)
    void reload(cur.id)
  }, [redactionDialogOpen, currentCaseId])

  const close = (): void => setRedactionDialogOpen(false)
  const cardRef = useDialogA11y(close, redactionDialogOpen)
  if (!redactionDialogOpen || !cur) return null

  const savePolicy = async (): Promise<void> => {
    if (policy === 'custom' && customTypes.length === 0) {
      toast('自定义档至少需要勾选一种敏感字段类型')
      return
    }
    setSaving(true)
    const ok = await setCaseRedactionPolicy(policy, customTypes)
    setSaving(false)
    if (ok) setDirty(false)
  }

  const confirmEntity = async (id: string, action: 'confirm' | 'reject'): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const r = await bridge.redaction.confirmEntities({ caseId: cur.id, entityIds: [id], action })
      toast(
        action === 'confirm'
          ? `已确认 ${r.count} 项实体（脱敏继续使用，写入审计）`
          : `已驳回 ${r.count} 项实体（该内容将不被替换）`
      )
      await reload(cur.id)
    } catch (e) {
      toast(`确认失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const addTerm = async (): Promise<void> => {
    const term = termInput.trim()
    const bridge = getBridge()
    if (!bridge || !term) return
    try {
      await bridge.redaction.addTerm({ caseId: cur.id, term })
      setTermInput('')
      await reload(cur.id)
      toast(`敏感词「${term}」已添加（需在自定义/严格档勾选「自定义敏感词」类型生效）`)
    } catch (e) {
      toast(`添加失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const removeTerm = async (termId: string, term: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      await bridge.redaction.removeTerm({ caseId: cur.id, termId })
      await reload(cur.id)
      toast(`敏感词「${term}」已删除`)
    } catch (e) {
      toast(`删除失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const toggleType = (key: string): void => {
    setCustomTypes((prev) => (prev.includes(key) ? prev.filter((x) => x !== key) : [...prev, key]))
    setDirty(true)
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="脱敏与隐私设置"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card redact-card">
        <div className="modal-head">
          <h3>脱敏与隐私设置 · {cur.title}</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>

        <div className="modal-body">
          <p className="form-hint">
            外部调用（远程模型）发送的内容先按当前档位替换为匿名别名，原文与别名映射仅保存在本机。
          </p>

          {/* 档位 */}
          <div className="form-row">
            <span className="form-label">脱敏档位</span>
            <div className="mode-radio" role="radiogroup" aria-label="脱敏档位">
              {(Object.keys(POLICY_LABEL) as RedactionPolicy[]).map((p) => (
                <button
                  key={p}
                  type="button"
                  role="radio"
                  aria-checked={policy === p}
                  className={`mode-radio-item${policy === p ? ' on' : ''}`}
                  onClick={() => {
                    setPolicy(p)
                    if (p !== 'custom') setCustomTypes(cur.redactionCustomTypes ?? [])
                    setDirty(true)
                  }}
                >
                  {POLICY_LABEL[p]}
                </button>
              ))}
            </div>
            <p className="form-hint">{POLICY_DESC[policy]}</p>
          </div>

          {/* custom 勾选 */}
          {policy === 'custom' && (
            <div className="form-row">
              <span className="form-label">需要替换的敏感字段类型</span>
              <div className="type-checks">
                {ENTITY_TYPES.map((t) => (
                  <label key={t.key} className={`type-check${customTypes.includes(t.key) ? ' on' : ''}`}>
                    <input
                      type="checkbox"
                      checked={customTypes.includes(t.key)}
                      onChange={() => toggleType(t.key)}
                    />
                    {t.label}
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* 待人工确认实体（REQ-PRIV-006：未确认阻断外发） */}
          <div className="form-row">
            <span className="form-label">
              待人工确认实体（{entities.length}）
              {entities.length > 0 && <em> · 未确认前外发将被阻断</em>}
            </span>
            {entities.length === 0 ? (
              <p className="form-hint">当前没有低置信度实体；全部实体已按档位自动替换。</p>
            ) : (
              <div className="entity-list">
                {entities.map((e) => (
                  <div key={e.id} className="entity-row">
                    <span className="entity-type">{e.typeLabel}</span>
                    <span className="entity-original" title={e.original}>
                      {e.original}
                    </span>
                    <span className="entity-conf">{Math.round(e.confidence * 100)}%</span>
                    <button className="btn-ghost btn-mini" onClick={() => void confirmEntity(e.id, 'confirm')}>
                      确认
                    </button>
                    <button className="btn-ghost btn-mini" onClick={() => void confirmEntity(e.id, 'reject')}>
                      驳回
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 自定义敏感词 */}
          <div className="form-row">
            <span className="form-label">自定义敏感词（{terms.length}）</span>
            <div className="term-add-row">
              <input
                className="form-input"
                placeholder="如：公司内部项目代号、特定人名"
                value={termInput}
                onChange={(e) => setTermInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void addTerm()
                }}
              />
              <button className="btn-ghost" onClick={() => void addTerm()}>
                添加
              </button>
            </div>
            {terms.length > 0 && (
              <div className="term-list">
                {terms.map((t) => (
                  <span key={t.id} className="term-chip">
                    {t.term}
                    <button aria-label={`删除 ${t.term}`} onClick={() => void removeTerm(t.id, t.term)}>
                      <Icon id="i-x" />
                    </button>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="modal-foot">
          <button className="btn-ghost" onClick={close}>
            关闭
          </button>
          <button className="btn-primary" disabled={!dirty || saving} onClick={() => void savePolicy()}>
            保存档位
          </button>
        </div>
      </div>
    </div>
  )
}
