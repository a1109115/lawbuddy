import { useEffect, useMemo, useState, type JSX } from 'react'
import type {
  DocGenBody,
  DocGenDraft,
  DocGenDraftVersion,
  DocGenLayout,
  DocGenPreview,
  TextDiffResult
} from '@lawbuddy/contracts'
import { useEscapeKey } from '../hooks/useDialogA11y'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { CiteBadge } from './CiteBadge'
import { DiffPanel } from './DiffPanel'
import { DraftBodyEditor } from './preview/DraftBodyEditor'
import { Icon } from './Icon'

const SOURCE_TYPE_LABEL: Record<string, string> = {
  case_evidence: '案件原文',
  user_confirmed: '用户确认',
  pending_verify: '待核实'
}

const STATUS_LABEL: Record<string, string> = {
  outline: '提纲待确认',
  draft: '草稿',
  confirmed: '已定稿'
}

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/**
 * 文书工作台（升级方案 §7.1 / 阶段二）：作为右侧预览区的一等视图，与中间区对话并存。
 * 排版规范 / 使用提示 / 本次产物 → 生成正文 / 定稿 / 导出 / 版本。
 * 文书结构、提纲、模板字段、正文预览与 AI 修订说明不在工作台展示；字段信息统一由底部输入坞（对话）补充。
 */
export function DocGenWorkbench({ onBack }: { onBack?: () => void }): JSX.Element | null {
  const docgenDraftId = useApp((s) => s.docgenDraftId)
  const openDocGen = useApp((s) => s.openDocGen)
  const toast = useApp((s) => s.toast)
  if (!docgenDraftId) return null
  return (
    <DocGenDraftView
      draftId={docgenDraftId}
      onBack={onBack ?? (() => openDocGen(null))}
      toast={toast}
    />
  )
}

/** 单个草稿的编辑视图（提纲 / 槽位 / 正文 / 操作栏 / 版本历史） */
function DocGenDraftView({
  draftId,
  onBack,
  toast
}: {
  draftId: string
  onBack: () => void
  toast: (text: string) => void
}): JSX.Element {
  const writable = useApp((s) => {
    const current = [...s.cases, ...s.archivedCases].find((c) => c.id === s.currentCaseId)
    return current?.defaultMode === 'document_generation' && current.status === 'active'
  })
  // 本次产物与右侧通用文档预览联动
  const artifacts = useApp((s) => s.artifacts)
  const loadArtifacts = useApp((s) => s.loadArtifacts)
  const openDoc = useApp((s) => s.openDoc)
  const curFileId = useApp((s) => s.curFileId)
  const [draft, setDraft] = useState<DocGenDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<DocGenDraftVersion[] | null>(null)
  // 导出格式（G-008：Markdown / Word / PDF）
  const [exportFmt, setExportFmt] = useState<'md' | 'docx' | 'pdf' | 'xlsx' | 'pptx'>('md')
  // —— 版本比较（REQ-GEN-010） ——
  const [diff, setDiff] = useState<TextDiffResult | null>(null)
  // 正文手动编辑（Markdown 源稿；槽位与定稿校验仍由 Core 负责）
  const [editingBody, setEditingBody] = useState(false)
  // DOCX 模板（升级方案 §6.2）：按原格式生成 Word 的入口只在 DOCX 模板草稿上出现
  const [docxMode, setDocxMode] = useState<'strict' | 'reference' | null>(null)
  const currentCaseId = useApp((s) => s.currentCaseId)

  useEscapeKey(() => setVersionsOpen(false), versionsOpen)

  useEffect(() => {
    void loadArtifacts()
  }, [draftId, loadArtifacts])

  const locked = !writable || draft?.status === 'confirmed'

  // 本次产物（同一草稿的 Word/PDF 等文件卡，最新在前）
  const products = useMemo(
    () =>
      [...artifacts]
        .filter((artifact) => artifact.docgenDraftId === draftId)
        .sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || '')),
    [artifacts, draftId]
  )

  /** 打开本次产物：复用右侧通用文档预览（原件渲染 + 编辑） */
  const openProduct = (artifactId: string): void => openDoc(artifactId)

  const applyDraft = (d: DocGenDraft): void => {
    setDraft(d)
  }

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge) return
    let alive = true
    bridge.docgen
      .getDraft(draftId)
      .then((d) => {
        if (alive) applyDraft(d)
      })
      .catch((e) => {
        if (alive) setError(`文书加载失败：${errText(e)}`)
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftId])

  const run = async (fn: () => Promise<DocGenDraft>): Promise<void> => {
    if (busy || !writable) return
    setBusy(true)
    setError('')
    try {
      applyDraft(await fn())
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  const bridge = (): NonNullable<ReturnType<typeof getBridge>> => {
    const b = getBridge()
    if (!b) throw new Error('桥不可用')
    return b
  }

  // —— 正文 / 定稿 / 导出 ——
  const renderBody = (): void => void run(() => bridge().docgen.render(draftId))
  const confirmOutline = (): void => void run(() => bridge().docgen.confirmOutline(draftId))
  const confirmDraft = (): void => {
    if (!draft || !window.confirm('请确认：已逐项核实当前正文中新增或改写的事实、金额、日期和法律依据。缺失或冲突信息不能作为已确认事实。')) return
    void run(() => bridge().docgen.confirm(draftId, { contentReviewed: true, expectedUpdatedAt: draft.updatedAt }))
  }

  const exportDraft = async (): Promise<void> => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const r = await bridge().docgen.export({ draftId, format: exportFmt })
      if (!r) {
        toast('已取消导出')
        return
      }
      toast(
        r.draftMarked
          ? `已导出草稿标识文件（含 ${r.blockingCount} 处待补充）：${r.path}`
          : `已导出正式文书：${r.path}`
      )
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  const loadVersions = (): void => {
    setVersionsOpen(true)
    void bridge()
      .docgen.listVersions(draftId)
      .then(setVersions)
      .catch((e) => toast(`版本加载失败：${errText(e)}`))
  }

  // 模板来源格式（DOCX 模板时工作台提供「按原格式生成」）
  useEffect(() => {
    const b = getBridge()
    if (!b || !draft?.templateId || !currentCaseId) {
      setDocxMode(null)
      return
    }
    let alive = true
    b.docgen
      .listTemplates(currentCaseId)
      .then((list) => {
        if (!alive) return
        const tpl = list.find((t) => t.rootId === draft.templateId || t.id === draft.templateId)
        setDocxMode(
          tpl?.sourceFormat === 'docx'
            ? (tpl.templateMode as 'strict' | 'reference')
            : null
        )
      })
      .catch(() => alive && setDocxMode(null))
    return () => {
      alive = false
    }
  }, [draft?.templateId, currentCaseId])

  /** 按 DOCX 模板原格式生成（升级方案 §6.2）：格式警告在结果里显式提示 */
  const renderByDocx = async (): Promise<void> => {
    if (busy || !writable) return
    setBusy(true)
    setError('')
    try {
      const r = await bridge().docgen.renderDocx({ draftId })
      applyDraft(r.draft)
      await loadArtifacts()
      toast(
        r.warnings.length
          ? `已按 Word 模板生成 v${r.versionNo}（${r.templateMode === 'strict' ? '变量替换' : '参考范本'}），${r.warnings.length} 条格式提醒`
          : `已按 Word 模板生成 v${r.versionNo}（${r.templateMode === 'strict' ? '变量替换' : '参考范本'}）`
      )
    } catch (e) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }
  const restoreVersion = (versionNo: number): void => {
    void run(() => bridge().docgen.restoreVersion({ draftId, versionNo }))
    setVersionsOpen(false)
  }

  // —— 版本比较（REQ-GEN-010） ——
  const compareVersion = (versionNo: number, toVersionNo?: number): void => {
    void bridge()
      .docgen.diffVersions({ draftId, fromVersionNo: versionNo, toVersionNo: toVersionNo ?? null })
      .then(setDiff)
      .catch((e) => toast(`版本比较失败：${errText(e)}`))
  }

  if (!draft) {
    return (
      <div className="rp-view dg-workbench" aria-label="文书工作台">
        <div className="dg-wb-inner">
          <div className="dg-head">
            <button className="icon-btn" aria-label="返回文档列表" title="返回文档列表" onClick={onBack}>
              <Icon id="i-chevl" />
            </button>
            <span className="dg-title">
              <Icon id="i-doc" />
              文书详情
            </span>
          </div>
          <div className="dg-empty">{error || '加载中…'}</div>
        </div>
      </div>
    )
  }

  const outlineConfirmed = draft.outlineConfirmed
  const preview = draft.preview

  return (
    <div className="rp-view dg-workbench" aria-label="文书工作台">
      <div className="dg-wb-inner">
      <div className="dg-head">
        <button className="icon-btn" aria-label="返回文档列表" title="返回文档列表" onClick={onBack}>
          <Icon id="i-chevl" />
        </button>
        <span className="dg-title">
          <Icon id="i-doc" />
          {draft.docType}起草
        </span>
        <span className={`st-badge dg-st-${draft.status}`}>{STATUS_LABEL[draft.status] ?? draft.status}</span>
        <span className="st-badge st-neutral" title="当前稿版本号：消息、文件卡、预览与恢复都以它为准">
          当前稿 v{draft.currentVersionNo}
        </span>
        <span className="dg-head-spacer" />
        {products.length > 0 && (
          <button className="cr-btn ok" onClick={() => openProduct(products[0].id)}>
            <Icon id="i-panel" />
            预览 Word
          </button>
        )}
      </div>
      <div className="dg-meta">
        {!writable && <span role="status">历史文书只读保留，当前任务不能编辑或修订。</span>}
        <span
          title={[draft.templateTitle ?? '', draft.templateContentSha256 ?? ''].filter(Boolean).join(' · ')}
        >
          模板：{draft.templateTitle ?? '已删除'}
          {draft.templateVersionNo != null ? ` v${draft.templateVersionNo}` : ''}
          {draft.templateContentSha256 ? ` · 哈希 ${draft.templateContentSha256.slice(0, 8)}` : ''}
        </span>
        {draft.purpose && <span className="dg-purpose-text">请求事项：{draft.purpose}</span>}
      </div>

      {/* —— 排版规范 / 使用提示（Core 确定性派生） —— */}
      {preview ? (
        <>
          <div className="dg-sec">
            <div className="dg-sec-h">
              <Icon id="i-doc" />
              排版规范
            </div>
            <LayoutSpec layout={preview.layout} />
          </div>

          {preview.usageTips.length > 0 && (
            <div className="dg-sec">
              <div className="dg-sec-h">
                <Icon id="i-bulb" />
                使用提示
              </div>
              <ol className="dg-usage-tips">
                {preview.usageTips.map((tip, i) => (
                  <li key={`${i}-${tip}`}>{tip}</li>
                ))}
              </ol>
            </div>
          )}
        </>
      ) : (
        <div className="dg-sec">
          <div className="dg-sec-h">
            <Icon id="i-alert" />
            结构化预览不可用
          </div>
          <div className="dg-empty-sm">当前本地服务未返回文书结构，可直接按模板生成正文，或在底部输入框补充要求。</div>
        </div>
      )}

      {/* —— 本次产物（Word 文件卡，点击在右侧通用文档预览中打开） —— */}
      <div className="dg-sec">
        <div className="dg-sec-h">
          <Icon id="i-folder" />
          查看本次产物
        </div>
        {products.length === 0 ? (
          <div className="dg-empty-sm">本次任务尚未生成 Word 产物；点击「生成正文」或等待任务完成后自动落盘。</div>
        ) : (
          <div className="dg-products">
            {products.map((product) => (
              <button
                key={product.id}
                className={`dg-product${curFileId === product.id ? ' on' : ''}`}
                onClick={() => openProduct(product.id)}
              >
                <span className="ftype docx">W</span>
                <span className="dg-product-copy">
                  <b>{product.title}</b>
                  <span>
                    {product.versionNo > 0 ? `v${product.versionNo} · ` : ''}
                    {(product.createdAt || '').slice(0, 16).replace('T', ' ')} · 点击在右侧预览
                  </span>
                </span>
                <Icon id="i-chevr" />
              </button>
            ))}
          </div>
        )}
      </div>

      {/* —— 版本比较结果（REQ-GEN-010） —— */}
      {diff && <DiffPanel diff={diff} onClose={() => setDiff(null)} />}

      {error && <div className="dg-error">{error}</div>}

      {/* —— 操作栏 —— */}
      <div className="dg-actions">
        {!outlineConfirmed && (
          <button
            className="cr-btn"
            disabled={busy || locked || !draft.outline?.length}
            title={draft.outline?.length ? '核对提纲后确认，确认后才能生成正文' : '尚无提纲可确认'}
            onClick={confirmOutline}
          >
            确认提纲
          </button>
        )}
        <button
          className="cr-btn ok"
          disabled={busy || locked || !outlineConfirmed}
          title={outlineConfirmed ? '按模板与槽位生成正文' : '提纲尚未确认，确认提纲后才能生成正文'}
          onClick={renderBody}
        >
          生成正文
        </button>
        {docxMode && (
          <button
            className="cr-btn ok"
            disabled={busy || locked || !outlineConfirmed}
            title={
              docxMode === 'strict'
                ? '按 DOCX 模板原格式生成：替换变量，保留页眉页脚、样式与表格，未填字段写 xx'
                : '按 DOCX 参考范本生成：以模板副本为基底写入正文，保留页面设置与页眉页脚'
            }
            onClick={() => void renderByDocx()}
          >
            按 Word 模板生成
          </button>
        )}
        <button
          className="cr-btn"
          disabled={busy || locked || !draft.bodyMd}
          title={draft.bodyMd ? '编辑正文源稿（Markdown），保存写入版本历史' : '生成正文后才能编辑'}
          onClick={() => setEditingBody(true)}
        >
          编辑正文
        </button>
        <button
          className="cr-btn ok"
          disabled={busy || locked || draft.blockingCount > 0 || !draft.bodyMd}
          title={
            draft.blockingCount > 0
              ? `正文存在 ${draft.blockingCount} 处待补充占位符，补全后才能定稿`
              : draft.bodyMd
                ? '定稿后可导出正式文书'
                : '尚无正文可定稿'
          }
          onClick={confirmDraft}
        >
          定稿
        </button>
        <select
          className="form-input dg-export-fmt"
          value={exportFmt}
          disabled={busy || !draft.bodyMd}
          aria-label="导出格式"
          onChange={(e) => setExportFmt(e.target.value as 'md' | 'docx' | 'pdf' | 'xlsx' | 'pptx')}
        >
          <option value="md">Markdown</option>
          <option value="docx">Word</option>
          <option value="pdf">PDF</option>
          <option value="xlsx">Excel</option>
          <option value="pptx">PPT</option>
        </select>
        <button
          className="cr-btn"
          disabled={busy || !draft.bodyMd}
          title={draft.bodyMd ? '选择目录导出（草稿态加「草稿_」前缀）' : '尚无正文可导出'}
          onClick={() => void exportDraft()}
        >
          导出
        </button>
        <button className="cr-btn" disabled={busy} onClick={loadVersions}>
          版本历史
        </button>
      </div>

      {/* —— 版本历史抽屉 —— */}
      {versionsOpen && (
        <div className="dg-versions" role="dialog" aria-label="版本历史">
          <div className="dg-sec-h">
            <Icon id="i-clock" />
            版本历史
            <button className="icon-btn dg-versions-x" aria-label="关闭版本历史" onClick={() => setVersionsOpen(false)}>
              <Icon id="i-x" />
            </button>
          </div>
          {versions === null ? (
            <div className="dg-empty-sm">加载中…</div>
          ) : versions.length === 0 ? (
            <div className="dg-empty-sm">尚无历史版本（生成、编辑与自动保存都会落版本）</div>
          ) : (
            versions.map((v, i) => (
              <div key={v.versionNo} className="dg-ver-row">
                <b>v{v.versionNo}</b>
                <span className={`dg-ver-kind dg-ver-${v.kind}`}>{v.kindLabel}</span>
                <span>{(v.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
                <span className={v.blocking.length > 0 ? 'dg-ver-block' : ''}>
                  占位符 {v.blocking.reduce((n, b) => n + b.count, 0)} 处
                </span>
                <span className="dg-ver-chars">{v.charCount} 字</span>
                <button className="cr-btn" disabled={busy} onClick={() => compareVersion(v.versionNo)}>
                  与当前稿比较
                </button>
                {i + 1 < versions.length && (
                  <button
                    className="cr-btn"
                    disabled={busy}
                    onClick={() => compareVersion(versions[i + 1].versionNo, v.versionNo)}
                  >
                    与上一版本比较
                  </button>
                )}
                <button className="cr-btn" disabled={busy || !writable} onClick={() => restoreVersion(v.versionNo)}>
                  恢复此版本
                </button>
              </div>
            ))
          )}
        </div>
      )}
      {editingBody && (
        <DraftBodyEditor
          draftId={draft.id}
          title={draft.docType}
          initialBody={draft.bodyMd ?? ''}
          baseVersionNo={draft.currentVersionNo}
          onClose={() => setEditingBody(false)}
          onSaved={applyDraft}
        />
      )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 消息流内的文书生成结果渲染（body.docgen 分支，BodyView 调用）
// ---------------------------------------------------------------------------

/** 排版规范列表（A4 / 页边距 / 标题 / 正文；工作台与对话卡共用） */
function LayoutSpec({ layout }: { layout: DocGenLayout }): JSX.Element {
  const fontLabel = (name: string): string =>
    ({ SimSun: '宋体', FangSong: '仿宋', KaiTi: '楷体', 'Microsoft YaHei': '微软雅黑', 'Times New Roman': 'Times New Roman' })[name] ?? name
  return (
    <ul className="dg-layout-list">
      <li>纸张：{layout.paper}（{layout.paperSizeMm.width}mm × {layout.paperSizeMm.height}mm）</li>
      <li>
        页边距：上 {layout.marginsCm.top}cm、下 {layout.marginsCm.bottom}cm、左 {layout.marginsCm.left}cm、右{' '}
        {layout.marginsCm.right}cm
      </li>
      <li>
        标题：{fontLabel(layout.titleStyle.fontFamily)} {layout.titleStyle.fontSize}
        {layout.titleStyle.bold ? '加粗' : ''}，居中
      </li>
      <li>
        正文：{fontLabel(layout.bodyStyle.fontFamily)} {layout.bodyStyle.fontSize}，{layout.bodyStyle.lineHeight} 倍行距
      </li>
    </ul>
  )
}

/** 文书生成结果摘要：文书结构 / 排版规范 / 使用提示 / 本次产物 + 打开文书入口（PRD 15.4） */
export function DocGenMessageView({ body }: { body: DocGenBody }): JSX.Element {
  const openDocGen = useApp((s) => s.openDocGen)
  const openCitation = useApp((s) => s.openCitation)
  const openClaimsQueue = useApp((s) => s.openClaimsQueue)
  const openDoc = useApp((s) => s.openDoc)
  const artifacts = useApp((s) => s.artifacts)
  const [preview, setPreview] = useState<DocGenPreview | null>(null)
  // 当前稿版本号（版本锚点）：消息卡记录的 vN 落后时提示有新版本（升级方案 §7.4）
  const [currentVersionNo, setCurrentVersionNo] = useState<number | null>(null)

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge) return
    let alive = true
    bridge.docgen
      .getDraft(body.draftId)
      .then((draft) => {
        if (!alive) return
        setPreview(draft.preview ?? null)
        setCurrentVersionNo(draft.currentVersionNo ?? null)
      })
      .catch(() => {
        if (alive) {
          setPreview(null)
          setCurrentVersionNo(null)
        }
      })
    return () => {
      alive = false
    }
  }, [body.draftId])

  const products = useMemo(
    () =>
      [...artifacts]
        .filter((artifact) => artifact.docgenDraftId === body.draftId)
        .sort((a, b) => (b.createdAt || '').localeCompare(a.createdAt || '')),
    [artifacts, body.draftId]
  )

  return (
    <div className="dg-msg" aria-label="文书生成结果">
      <div className="dg-msg-head">
        <Icon id="i-doc" />
        {body.revision ? '文书 AI 修订' : '文书生成'}{body.docType ? ` · ${body.docType}` : ''}
        {body.revision ? (
          <span className={`st-badge ${body.revision.applied ? 'st-favorable' : 'fst-pending'}`}>
            {body.revision.applied
              ? `已落新版本 v${body.revision.versionNo ?? ''}（基于 v${body.revision.baseVersionNo ?? '?'}）`
              : body.revision.reason === 'version_conflict'
                ? '未落库：当前稿已被更新'
                : '未执行修订（本地模式需配置模型）'}
          </span>
        ) : (
          <span className={`st-badge ${body.outlineConfirmed ? 'st-favorable' : 'st-neutral'}`}>
            {body.outlineConfirmed ? '提纲已确认' : '提纲待确认'}
          </span>
        )}
        {typeof body.versionNo === 'number' && body.versionNo > 0 && (
          <span className="st-badge st-neutral" title="本条消息对应的草稿版本号">
            v{body.versionNo}
          </span>
        )}
        <button className="cr-btn ok dg-open" onClick={() => openDocGen(body.draftId)}>
          <Icon id="i-arrow" />
          打开文书
        </button>
      </div>

      {body.revision && (
        <div className="dg-msg-sec">
          <div className="dg-msg-label">修订意见</div>
          <div className="dg-msg-rev-instr">{body.revision.instruction}</div>
          {body.revision.applied ? (
            <div className="dg-msg-rev-note">
              {body.revision.notes
                ? `修订说明：${body.revision.notes}`
                : '修订稿已按意见生成并落为新版本，历史版本保留，可在版本历史中比较/恢复'}
            </div>
          ) : body.revision.reason === 'version_conflict' ? (
            <div className="dg-msg-rev-note dg-msg-rev-blocked">
              修订基于的版本已被推进（期间有手工编辑或另一轮修订落库），本次未覆盖当前稿。
              请基于最新稿重新提出修改意见；{body.revision.notes ? `（${body.revision.notes}）` : ''}
            </div>
          ) : (
            <div className="dg-msg-rev-note dg-msg-rev-blocked">
              本地模式未调用远程模型，当前稿保持不变、未落新版本；请在配置模型后重新发起修订
            </div>
          )}
        </div>
      )}

      {preview ? (
        <>
          <div className="dg-msg-sec">
            <div className="dg-msg-label">
              文书结构（{preview.sections.filter((s) => s.status === 'filled').length}/{preview.sections.length} 段已填）
            </div>
            <div className="dg-msg-struct">
              {preview.sections.map((section) => (
                <div key={section.key} className="dg-msg-struct-row">
                  <span className="dg-msg-struct-k">{section.label}</span>
                  <span className={`dg-msg-struct-v${section.status === 'pending' ? ' pending' : ''}`}>
                    {section.status === 'pending' ? '待补充' : section.value}
                  </span>
                  <span className="dg-msg-struct-meta">
                    {section.sourceType && (
                      <span className="st-badge st-neutral">
                        {SOURCE_TYPE_LABEL[section.sourceType] ?? section.sourceType}
                      </span>
                    )}
                    {section.sourceRef?.documentId && (
                      <CiteBadge
                        cite={{
                          documentId: section.sourceRef.documentId,
                          pageNo: section.sourceRef.pageNo ?? undefined,
                          label: section.sourceRef.documentTitle
                            ? `${section.sourceRef.documentTitle}${section.sourceRef.pageNo ? ` · 第${section.sourceRef.pageNo}页` : ''}`
                            : undefined
                        }}
                        onCite={openCitation}
                      />
                    )}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="dg-msg-sec">
            <div className="dg-msg-label">排版规范</div>
            <LayoutSpec layout={preview.layout} />
          </div>

          {preview.usageTips.length > 0 && (
            <div className="dg-msg-sec">
              <div className="dg-msg-label">使用提示</div>
              <ol className="dg-usage-tips">
                {preview.usageTips.map((tip, i) => (
                  <li key={`${i}-${tip}`}>{tip}</li>
                ))}
              </ol>
            </div>
          )}

          {products.length > 0 && (
            <div className="dg-msg-sec">
              <div className="dg-msg-label">本次产物</div>
              <div className="dg-msg-products">
                {products.map((product) => (
                  <button key={product.id} className="dg-msg-product" onClick={() => openDoc(product.id)}>
                    <span className="ftype docx">W</span>
                    <span className="dg-msg-product-copy">
                      <b>{product.title}</b>
                      <span>{(product.createdAt || '').slice(0, 16).replace('T', ' ')} · 点击在右侧预览</span>
                    </span>
                    <Icon id="i-chevr" />
                  </button>
                ))}
              </div>
            </div>
          )}
        </>
      ) : (
        <>
          {body.outline && body.outline.length > 0 && (
            <div className="dg-msg-sec">
              <div className="dg-msg-label">建议提纲（{body.outline.length} 章，可在文书中修改）</div>
              <ol className="dg-msg-outline">
                {body.outline.map((o, i) => (
                  <li key={`${o.heading}-${i}`}>
                    <b>{o.heading}</b>
                    {o.points && <span> —— {o.points}</span>}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {body.slots.length > 0 && (
            <div className="dg-msg-sec">
              <div className="dg-msg-label">字段建议（{body.slots.length} 项）</div>
              <div className="dg-msg-slots">
                {body.slots.map((s, i) => (
                  <div key={`${s.name}-${i}`} className="dg-msg-slot">
                    <b className="dg-slot-name">{s.name}</b>
                    <span className="dg-msg-val">{(s.value ?? '').slice(0, 48) || '（待补充）'}</span>
                    <span className={`st-badge ${s.sourceType === 'pending_verify' ? 'fst-pending' : 'st-neutral'}`}>
                      {s.sourceTypeLabel ?? SOURCE_TYPE_LABEL[s.sourceType] ?? s.sourceType}
                    </span>
                    {s.sourceRef?.documentId && (
                      <CiteBadge
                        cite={{
                          documentId: s.sourceRef.documentId,
                          pageNo: s.sourceRef.pageNo ?? undefined,
                          label: s.sourceRef.documentTitle
                            ? `${s.sourceRef.documentTitle}${s.sourceRef.pageNo ? ` · 第${s.sourceRef.pageNo}页` : ''}`
                            : undefined
                        }}
                        onCite={openCitation}
                        onPending={openClaimsQueue}
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {(preview?.missingFields.length ?? body.missingFields.length) > 0 && (
        <div className="dg-msg-missing">
          <Icon id="i-alert" />
          缺失字段{' '}
          {(preview?.missingFields.length ?? body.missingFields.length)} 项：
          {(preview?.missingFields ?? body.missingFields).join('、')}（正文以 xx 标注，不编造）
        </div>
      )}

      {currentVersionNo !== null &&
        typeof body.versionNo === 'number' &&
        currentVersionNo > body.versionNo && (
          <div className="dg-msg-stale" role="status">
            <Icon id="i-alert" />
            当前稿已推进到 v{currentVersionNo}（本条结果基于 v{body.versionNo}）。
            继续修改请以当前稿为准，旧版本可在版本历史中比较与恢复。
          </div>
        )}

      <div className="dg-msg-audit">以上为 AI 生成的文书草稿，关键事实请以原文为准；待补充信息不会自动编造。</div>
    </div>
  )
}
