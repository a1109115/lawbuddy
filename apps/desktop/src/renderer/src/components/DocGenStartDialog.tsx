import { useEffect, useRef, useState, type JSX } from 'react'
import type {
  DocGenStartInput,
  DocGenTemplate,
  DocGenTemplateDiffResult,
  DocGenTemplateVersion
} from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { DiffPanel } from './DiffPanel'
import { Icon } from './Icon'

/**
 * 文书生成发起对话框（PRD 15.2 输入表）：
 * 选择文书模板 / 文书类型 / 请求事项，经 agent.start.docgenInput 通道发起；
 * 模板与文书类型不匹配时需用户确认（confirmMismatch）后重发（PRD 15.6）。
 * 模板版本（REQ-TPL-005 / G-002）：可选版本生成、更新为新版本、版本比较与回退
 * （回退仅移动当前版本指针，较新版本保留）。
 */
export function DocGenStartDialog(): JSX.Element | null {
  const { docgenStartOpen, setDocgenStartOpen, currentCaseId, input, startDocGenTask, toast } = useApp()

  const [templates, setTemplates] = useState<DocGenTemplate[]>([])
  const [loading, setLoading] = useState(false)
  const [templateId, setTemplateId] = useState('')
  const [docType, setDocType] = useState('')
  const [purpose, setPurpose] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [mismatch, setMismatch] = useState(false)
  // —— 导入模板表单（展开式；updateMode 时作为所选模板的新版本导入） ——
  const [importOpen, setImportOpen] = useState(false)
  const [updateMode, setUpdateMode] = useState(false)
  const [impTitle, setImpTitle] = useState('')
  const [impDocType, setImpDocType] = useState('')
  const [impMd, setImpMd] = useState('')
  const [impBusy, setImpBusy] = useState(false)
  const [impError, setImpError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  // —— 模板版本（REQ-TPL-005 / G-002） ——
  const [versions, setVersions] = useState<DocGenTemplateVersion[]>([])
  const [versionId, setVersionId] = useState('') // 生成所用版本行 id（默认=当前版本）
  const [verOpen, setVerOpen] = useState(false)
  const [verBusy, setVerBusy] = useState(false)
  const [rollbackConfirm, setRollbackConfirm] = useState(0) // 两步确认：待回退 version_no
  const [diff, setDiff] = useState<DocGenTemplateDiffResult | null>(null)
  const diffOpenRef = useRef(false)
  diffOpenRef.current = diff !== null

  // 打开时拉取模板清单（案件模板 + 全局模板），回落选中首个 active 模板
  useEffect(() => {
    if (!docgenStartOpen) return
    setError('')
    setMismatch(false)
    setImportOpen(false)
    setUpdateMode(false)
    setVerOpen(false)
    setRollbackConfirm(0)
    setDiff(null)
    setDocxName('')
    setDocxBase64('')
    setDocxWarnings([])
    const bridge = getBridge()
    if (!bridge || !currentCaseId) return
    setLoading(true)
    bridge.docgen
      .listTemplates(currentCaseId)
      .then((list) => {
        setTemplates(list)
        setTemplateId((cur) => {
          if (cur && list.some((t) => t.id === cur && t.status === 'active')) return cur
          return (list.find((t) => t.status === 'active') ?? list[0])?.id ?? ''
        })
      })
      .catch((e) => setError(`模板加载失败：${e instanceof Error ? e.message : String(e)}`))
      .finally(() => setLoading(false))
  }, [docgenStartOpen, currentCaseId])

  // 所选模板的版本组历史（版本选择默认当前版本；REQ-TPL-005）
  const loadVersions = (baseId: string, preferId?: string): void => {
    const bridge = getBridge()
    if (!bridge || !baseId) {
      setVersions([])
      return
    }
    bridge.docgen
      .listTemplateVersions(baseId)
      .then((list) => {
        setVersions(list)
        setVersionId((prev) => {
          if (preferId) return preferId
          if (prev && list.some((v) => v.id === prev && v.status === 'active')) return prev
          return (list.find((v) => v.isCurrent && v.status === 'active') ?? list[0])?.id ?? ''
        })
      })
      .catch(() => setVersions([]))
  }

  // 选择模板后默认带出其文书类型与版本组（默认用当前版本生成）
  const pickTemplate = (id: string): void => {
    setTemplateId(id)
    setVersionId(id)
    setRollbackConfirm(0)
    setDiff(null)
    const tpl = templates.find((t) => t.id === id)
    setDocType(tpl?.docType ?? '')
    loadVersions(id)
  }

  const close = (): void => {
    // Esc 分层：版本比较面板先于对话框关闭（DEV-UI-008 层级约定）
    if (diffOpenRef.current) return
    setDocgenStartOpen(false)
  }
  const cardRef = useDialogA11y(close, docgenStartOpen)
  if (!docgenStartOpen) return null

  const selected = templates.find((t) => t.id === templateId)

  const submit = async (confirmMismatch: boolean): Promise<void> => {
    setError('')
    setMismatch(false)
    const chosenId = versionId || templateId
    if (!chosenId) {
      setError('请选择文书模板')
      return
    }
    // 文书类型与请求事项均为选填：缺省用模板类型与当前输入，未填写不阻断生成
    const finalDocType = docType.trim() || selected?.docType?.trim() || '法律文书'
    const finalPurpose = purpose.trim() || input.trim() || `起草${finalDocType}`
    const inp: DocGenStartInput = {
      templateId: chosenId,
      docType: finalDocType,
      purpose: finalPurpose,
      ...(confirmMismatch ? { confirmMismatch: true } : {})
    }
    setBusy(true)
    const r = await startDocGenTask(input.trim() || `起草${finalDocType}`, inp)
    setBusy(false)
    if (r.ok) return
    // 类型不匹配：弹确认，用户确认后带 confirmMismatch 重发（错误消息与 Core 对齐）
    if (!confirmMismatch && (r.error ?? '').includes('不匹配')) {
      setMismatch(true)
      setError(r.error ?? '')
      return
    }
    setError(r.error ?? '任务发起失败')
  }

  const readImportFile = (file: File): void => {
    const reader = new FileReader()
    reader.onload = () => {
      setImpMd(String(reader.result ?? ''))
      if (!impTitle.trim()) setImpTitle(file.name.replace(/\.(md|markdown|txt)$/i, ''))
    }
    reader.readAsText(file, 'utf-8')
  }

  // —— Word 模板上传（升级方案 §6.1）：文件选择在 Main 进程，原件只读保存 ——
  const [docxName, setDocxName] = useState('')
  const [docxBase64, setDocxBase64] = useState('')
  const [docxWarnings, setDocxWarnings] = useState<string[]>([])

  const pickDocx = async (): Promise<void> => {
    setImpError('')
    const bridge = getBridge()
    if (!bridge) {
      setImpError('当前为浏览器调试模式，无法上传模板')
      return
    }
    try {
      const picked = await bridge.docgen.pickDocxTemplate()
      if (!picked) return
      setDocxName(picked.name)
      setDocxBase64(picked.fileBase64)
      setDocxWarnings([])
      if (!impTitle.trim()) setImpTitle(picked.name.replace(/\.docx$/i, ''))
    } catch (e) {
      setImpError(`选择文件失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const doImportDocx = async (): Promise<void> => {
    setImpError('')
    const bridge = getBridge()
    if (!bridge) {
      setImpError('当前为浏览器调试模式，无法上传模板')
      return
    }
    if (!impTitle.trim()) {
      setImpError('请填写模板标题')
      return
    }
    if (!impDocType.trim()) {
      setImpError('请填写文书类型')
      return
    }
    if (!docxBase64) {
      setImpError('请先选择 .docx 模板文件')
      return
    }
    setImpBusy(true)
    try {
      const res = await bridge.docgen.importDocxTemplate({
        caseId: currentCaseId ?? undefined,
        title: impTitle.trim(),
        docType: impDocType.trim(),
        fileBase64: docxBase64,
        sourceName: docxName || undefined,
        ...(updateMode && selected ? { baseTemplateId: selected.id } : {})
      })
      const list = await bridge.docgen.listTemplates(currentCaseId ?? '')
      setTemplates(list)
      const tpl = res.template
      setTemplateId(tpl.id)
      setVersionId(tpl.id)
      setDocType(tpl.docType)
      loadVersions(tpl.id)
      // 格式警告与"无变量按范本处理"提示保留在界面上，导入成功后仍可见
      setDocxWarnings(res.warnings)
      setDocxBase64('')
      setDocxName('')
      if (!updateMode) {
        setImportOpen(false)
        setImpTitle('')
        setImpDocType('')
      }
      toast(
        `已导入 Word 模板「${tpl.title}」· ${tpl.templateMode === 'strict' ? '变量替换' : '参考范本'} · ${tpl.fields.length} 个字段`
      )
    } catch (e) {
      setImpError(`导入 Word 模板失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setImpBusy(false)
    }
  }

  const enterUpdateMode = (): void => {
    if (!selected) return
    setUpdateMode(true)
    setImportOpen(true)
    setImpTitle(selected.title)
    setImpDocType(selected.docType)
    setImpError('')
  }

  const doImport = async (): Promise<void> => {
    setImpError('')
    const bridge = getBridge()
    if (!bridge) {
      setImpError('当前为浏览器调试模式，无法导入模板')
      return
    }
    if (!impTitle.trim()) {
      setImpError('请填写模板标题')
      return
    }
    if (!impDocType.trim()) {
      setImpError('请填写文书类型')
      return
    }
    if (!impMd.trim()) {
      setImpError('请粘贴模板内容或选择本地 .md/.txt 文件')
      return
    }
    setImpBusy(true)
    try {
      const tpl = await bridge.docgen.importTemplate({
        caseId: currentCaseId ?? undefined,
        title: impTitle.trim(),
        docType: impDocType.trim(),
        templateMd: impMd,
        sourceName: fileRef.current?.files?.[0]?.name ?? undefined,
        ...(updateMode && selected ? { baseTemplateId: selected.id } : {})
      })
      // 刷新清单（解析失败也会产生独立留痕行，REQ-TPL-004）
      const list = await bridge.docgen.listTemplates(currentCaseId ?? '')
      setTemplates(list)
      if (tpl.status === 'parse_failed') {
        // 解析失败不抛错：保留原文并提示行号（REQ-TPL-003），可修正后重新导入
        setImpError(`模板解析失败：${tpl.parseError ?? '未知错误'}`)
        return
      }
      setTemplateId(tpl.id)
      setVersionId(tpl.id)
      setDocType(tpl.docType)
      loadVersions(tpl.id)
      setImportOpen(false)
      setUpdateMode(false)
      setImpTitle('')
      setImpDocType('')
      setImpMd('')
      if (fileRef.current) fileRef.current.value = ''
      toast(
        updateMode
          ? `模板已更新到 v${tpl.versionNo}，历史版本保留`
          : `模板「${tpl.title}」已导入，共 ${tpl.fields.length} 个字段`
      )
    } catch (e) {
      setImpError(`导入失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setImpBusy(false)
    }
  }

  // 与组内当前版本比较（REQ-TPL-005；差异结构与草稿版本比较一致）
  const doVerDiff = async (fromVersionNo: number): Promise<void> => {
    const bridge = getBridge()
    if (!bridge || !selected) return
    setVerBusy(true)
    try {
      const d = await bridge.docgen.diffTemplateVersions({
        templateId: selected.id,
        fromVersionNo
      })
      setDiff(d)
    } catch (e) {
      setError(`版本比较失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setVerBusy(false)
    }
  }

  const doRollback = async (v: DocGenTemplateVersion): Promise<void> => {
    const bridge = getBridge()
    if (!bridge || !selected) return
    setVerBusy(true)
    try {
      const cur = await bridge.docgen.rollbackTemplate({ templateId: selected.id, versionNo: v.versionNo })
      const list = await bridge.docgen.listTemplates(currentCaseId ?? '')
      setTemplates(list)
      setTemplateId(cur.id)
      setVersionId(cur.id)
      setDocType(cur.docType)
      setRollbackConfirm(0)
      loadVersions(cur.id)
      toast(`已回退到 v${cur.versionNo}，较新版本保留`)
    } catch (e) {
      setError(`回退失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setVerBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={close}>
      <div ref={cardRef} tabIndex={-1} className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>发起文书生成</h3>
          <button className="icon-btn" aria-label="关闭" onClick={close}>
            <Icon id="i-x" />
          </button>
        </div>
        <div className="modal-body">
          <div className="form-row">
            <span className="form-label">
              文书模板<em>*</em>
              <button
                type="button"
                className="dg-import-toggle"
                onClick={() => setImportOpen(!importOpen)}
              >
                <Icon id={importOpen ? 'i-minus' : 'i-plus'} />
                {importOpen ? '收起导入' : '导入模板'}
              </button>
            </span>
            {loading ? (
              <div className="cr-empty">模板加载中…</div>
            ) : templates.length === 0 ? (
              <div className="cr-empty">
                尚无可用模板，请先「导入模板」（上传自己的 Word 范本，或粘贴 Markdown / 选择本地 .md/.txt 文件）
              </div>
            ) : (
              <select className="form-input" value={templateId} onChange={(e) => pickTemplate(e.target.value)}>
                <option value="">请选择模板…</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id} disabled={t.status !== 'active'}>
                    {t.title}（{t.docType}）v{t.versionNo}{t.status !== 'active' ? ' · 不可用' : ''}
                  </option>
                ))}
              </select>
            )}
          </div>

          {selected && selected.status === 'active' && (
            <div className="dg-ver-bar">
              <button
                type="button"
                className="dg-import-toggle"
                onClick={() => {
                  setVerOpen(!verOpen)
                  if (!verOpen && versions.length === 0) loadVersions(selected.id)
                }}
              >
                <Icon id={verOpen ? 'i-minus' : 'i-list'} />
                版本历史（当前 v{selected.versionNo}）
              </button>
              <button type="button" className="dg-import-toggle" onClick={enterUpdateMode}>
                <Icon id="i-plus" />
                更新模板
              </button>
            </div>
          )}

          {/* 生成用版本选择（G-002：更新模板后可选历史版本生成） */}
          {selected && versions.length > 1 && (
            <div className="form-row">
              <span className="form-label">生成用模板版本</span>
              <select className="form-input" value={versionId} onChange={(e) => setVersionId(e.target.value)}>
                {versions
                  .filter((v) => v.status === 'active')
                  .map((v) => (
                    <option key={v.id} value={v.id}>
                      v{v.versionNo}
                      {v.isCurrent ? '（当前）' : ''} · {v.charCount} 字 · {(v.createdAt || '').slice(0, 10)}
                    </option>
                  ))}
              </select>
            </div>
          )}

          {/* 版本历史与回退（REQ-TPL-005：回退仅移动指针，较新版本保留） */}
          {verOpen && (
            <div className="dg-vers" role="region" aria-label="模板版本历史">
              <div className="dg-vers-head">
                <span>版本历史（{versions.length}）</span>
                <span className="dg-vers-tip">回退不删除较新版本</span>
              </div>
              {versions.length === 0 ? (
                <div className="cr-empty">版本加载中…</div>
              ) : (
                versions.map((v) => (
                  <div key={v.id} className={`dg-ver${v.isCurrent ? ' cur' : ''}`}>
                    <div className="dg-ver-line">
                      <b title={v.sourceSha256 ?? ''}>v{v.versionNo}{v.isCurrent ? ' · 当前' : ''}</b>
                      <span className="dg-ver-meta">
                        {v.status === 'active' ? `${v.fields.length} 字段 · ${v.charCount} 字` : '解析失败'}
                      </span>
                      <span className="dg-ver-time">{(v.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
                    </div>
                    {!v.isCurrent && v.status === 'active' && (
                      <div className="dg-ver-ops">
                        <button className="cr-btn" disabled={verBusy} onClick={() => void doVerDiff(v.versionNo)}>
                          与当前比较
                        </button>
                        {rollbackConfirm === v.versionNo ? (
                          <>
                            <button
                              className="cr-btn ok"
                              disabled={verBusy}
                              onClick={() => void doRollback(v)}
                            >
                              确认回退
                            </button>
                            <button className="cr-btn" onClick={() => setRollbackConfirm(0)}>
                              取消
                            </button>
                          </>
                        ) : (
                          <button
                            className="cr-btn"
                            disabled={verBusy}
                            onClick={() => setRollbackConfirm(v.versionNo)}
                          >
                            回退到此版本
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          )}

          {importOpen && (
            <div className="dg-import">
              {updateMode && selected && (
                <div className="dg-ver-update-banner">
                  将作为「{selected.title}」的新版本导入（当前 v{selected.versionNo}，历史版本保留）
                </div>
              )}
              <div className="form-row">
                <span className="form-label">模板标题<em>*</em></span>
                <input
                  className="form-input"
                  placeholder="如：民事起诉状（标准版）"
                  value={impTitle}
                  onChange={(e) => setImpTitle(e.target.value)}
                />
              </div>
              <div className="form-row">
                <span className="form-label">文书类型<em>*</em></span>
                <input
                  className="form-input"
                  placeholder="如：民事起诉状"
                  value={impDocType}
                  onChange={(e) => setImpDocType(e.target.value)}
                />
              </div>
              <div className="form-row">
                <span className="form-label">
                  模板内容（Markdown，字段用 {'{{字段名}}'} 或 【字段名】 占位）
                </span>
                <textarea
                  className="dg-imp-md"
                  rows={7}
                  placeholder={'# 民事起诉状\n原告：{{原告姓名}}\n诉讼请求：\n1. 判令被告偿还借款本金 {{借款本金}}…'}
                  value={impMd}
                  onChange={(e) => setImpMd(e.target.value)}
                />
                <input
                  ref={fileRef}
                  type="file"
                  accept=".md,.markdown,.txt"
                  className="dg-imp-file"
                  onChange={(e) => {
                    const f = e.target.files?.[0]
                    if (f) readImportFile(f)
                  }}
                />
              </div>

              {/* 上传自己的 Word 范本（升级方案 §6.1）：原件只读保存，生成时按原格式输出 */}
              <div className="dg-docx-import" role="region" aria-label="上传 Word 模板">
                <div className="dg-docx-head">
                  <Icon id="i-doc" />
                  <b>上传自己的 Word 模板（.docx）</b>
                  <span>原件只读保存，生成时保留页眉页脚与样式</span>
                </div>
                <div className="dg-docx-ops">
                  <button type="button" className="cr-btn" disabled={impBusy} onClick={() => void pickDocx()}>
                    <Icon id="i-upload" />
                    {docxName ? '重新选择文件' : '选择 .docx 文件'}
                  </button>
                  {docxName && <span className="dg-docx-name">{docxName}</span>}
                  <button
                    type="button"
                    className="cr-btn ok"
                    disabled={impBusy || !docxBase64}
                    onClick={() => void doImportDocx()}
                  >
                    {impBusy ? '解析中…' : '按 Word 模板导入'}
                  </button>
                </div>
                <p className="dg-docx-tip">
                  含 {'{{字段}}'} 或 【字段】 占位时按变量替换；未检测到变量则按参考范本生成新正文。
                  未填写字段统一以 xx 保留，定稿前需人工核实。
                </p>
                {docxWarnings.length > 0 && (
                  <ul className="dg-docx-warn" aria-label="模板格式警告">
                    {docxWarnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                )}
              </div>
              {impError && <div className="cr-error">{impError}</div>}
              <div className="dg-imp-ops">
                <button className="cr-btn ok" disabled={impBusy} onClick={() => void doImport()}>
                  {impBusy ? '导入中…' : '导入 Markdown 模板'}
                </button>
              </div>
            </div>
          )}

          <div className="form-row">
            <span className="form-label">
              文书类型
              {selected && docType !== selected.docType && (
                <span className="dg-type-warn">所选模板为「{selected.docType}」模板</span>
              )}
            </span>
            <input
              className="form-input"
              placeholder={`选填，默认「${selected?.docType ?? '模板文书类型'}」`}
              value={docType}
              onChange={(e) => setDocType(e.target.value)}
            />
          </div>

          <div className="form-row">
            <span className="form-label">请求事项</span>
            <textarea
              className="dg-purpose"
              rows={3}
              placeholder="选填，未填写时使用输入坞内容或「起草+文书类型」"
              value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
            />
          </div>

          {mismatch && (
            <div className="dg-mismatch">
              <Icon id="i-alert" />
              <span>{error}</span>
              <button className="cr-btn ok" disabled={busy} onClick={() => void submit(true)}>
                仍要使用
              </button>
            </div>
          )}
          {!mismatch && error && <div className="cr-error">{error}</div>}

          {diff && <DiffPanel diff={diff} onClose={() => setDiff(null)} />}
        </div>
        <div className="modal-foot">
          <button className="btn-ghost" onClick={close}>
            取消
          </button>
          <button className="btn-primary" disabled={busy} onClick={() => void submit(false)}>
            {busy ? '发起中…' : '开始生成'}
          </button>
        </div>
      </div>
    </div>
  )
}
