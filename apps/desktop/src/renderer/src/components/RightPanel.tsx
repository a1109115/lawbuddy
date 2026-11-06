import { useEffect, useRef, useState, type JSX, type MouseEvent as RMouseEvent } from 'react'
import type { ArtifactExportFormat, ArtifactVersionItem, SearchHit } from '@lawbuddy/contracts'
import { getBridge } from '../services/bridge'
import { exportOptionsFor } from '../services/exportFormats'
import { badge, useApp, type DocMeta } from '../state/store'
import { DocGenWorkbench } from './DocGenView'
import { Icon } from './Icon'
import { ReadingView } from './ReadingView'

/** 解析进行中的文档状态（contracts DocumentStatus 子集）：列表轮询刷新与「解析中」徽标 */
const INFLIGHT_STATUS = new Set(['pending', 'security_check', 'decrypting', 'parsing', 'ocr_queued', 'indexing'])

/**
 * 兜底补启动的最小间隔：解析由 Core 守护线程负责，前端只在「确实长时间没有
 * 任何页进度」时才补一次 ocr.start，避免两端高频互相触发。
 */
const OCR_STALL_MS = 20_000

/** 文档解析状态徽标：解析中（含页进度）/ 已就绪 / 解析失败 / 部分可读 / 待解锁 */
function statusBadge(f: DocMeta): { text: string; cls: string; tip: string } {
  // 终态优先：失败/待解锁/失效必须先可见，不能被「还有待识别页」掩盖成永久「解析中」
  switch (f.status) {
    case 'failed':
      return { text: '解析失败', cls: ' err', tip: '解析或文字识别失败，可删除后重新导入' }
    case 'need_password':
      return { text: '待解锁', cls: ' warn', tip: '已加密，点击输入密码解锁' }
    case 'stale':
      return { text: '已失效', cls: ' warn', tip: '原文来源已变化，内容仅供参考' }
    case 'locked':
      return { text: '已锁定', cls: ' warn', tip: '文档已锁定' }
  }
  if (f.ocrPending > 0) {
    return {
      text: `解析中 ${f.pages - f.ocrPending}/${f.pages}`,
      cls: ' run',
      tip: `文字识别中：已完成 ${f.pages - f.ocrPending}/${f.pages} 页，完成后可全文检索`
    }
  }
  switch (f.status) {
    case 'ready':
      return { text: '已就绪', cls: ' ok', tip: '解析完成，可阅读与全文检索' }
    case 'partial':
      return { text: '部分可读', cls: ' warn', tip: '部分页面解析/识别失败，其余内容可用' }
  }
  return INFLIGHT_STATUS.has(f.status)
    ? { text: '解析中…', cls: ' run', tip: '文档解析中，完成后自动可用' }
    : { text: '', cls: '', tip: f.status }
}

/** 全文命中摘要：【】标记渲染为高亮 */
function Snippet({ text }: { text: string }): JSX.Element {
  const parts = text.split(/(【[^】]*】)/)
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith('【') ? (
          <mark key={i} className="hit-mark">
            {p.slice(1, -1)}
          </mark>
        ) : (
          <span key={i}>{p}</span>
        )
      )}
    </>
  )
}

/** 文档删除按钮（两步确认；R-007：受影响 Claim 失效，旧产物保留） */
function DelDocButton({ docId, docName }: { docId: string; docName: string }): JSX.Element {
  const deleteDocument = useApp((s) => s.deleteDocument)
  const [confirming, setConfirming] = useState(false)

  const onClick = (e: RMouseEvent): void => {
    e.stopPropagation()
    if (!confirming) {
      setConfirming(true)
      setTimeout(() => setConfirming(false), 3000)
      return
    }
    setConfirming(false)
    void deleteDocument(docId)
  }

  return (
    <button
      className={`file-del${confirming ? ' confirm' : ''}`}
      aria-label={`删除文档 ${docName}`}
      title={confirming ? `再次点击确认删除「${docName}」` : '删除文档（相关结论将标记失效）'}
      onClick={onClick}
    >
      <Icon id="i-x" />
      {confirming && <span className="file-del-text">确认</span>}
    </button>
  )
}

/** 产物移除按钮（两步确认）：调试残留等非交付物移出产物概览；数据与磁盘文件保留 */
function DismissArtButton({
  artifactId,
  artName,
  readonly
}: {
  artifactId: string
  artName: string
  readonly: boolean
}): JSX.Element {
  const loadArtifacts = useApp((s) => s.loadArtifacts)
  const toast = useApp((s) => s.toast)
  const [confirming, setConfirming] = useState(false)

  const onClick = (e: RMouseEvent): void => {
    e.stopPropagation()
    if (readonly) return
    if (!confirming) {
      setConfirming(true)
      setTimeout(() => setConfirming(false), 3000)
      return
    }
    setConfirming(false)
    const bridge = getBridge()
    if (!bridge) return
    bridge.artifact.dismiss(artifactId)
      .then(() => loadArtifacts())
      .then(() => toast(`已移除「${artName}」（数据保留，仅不再显示）`))
      .catch((err) => toast(`移除失败：${err instanceof Error ? err.message : String(err)}`))
  }

  return (
    <button
      className={`file-del${confirming ? ' confirm' : ''}`}
      aria-label={`移除产物 ${artName}`}
      title={
        readonly
          ? '案件已归档，无法移除'
          : confirming
            ? `再次点击确认移除「${artName}」`
            : '移除出产物概览（数据保留在磁盘与审计，不再显示）'
      }
      onClick={onClick}
    >
      <Icon id="i-x" />
      {confirming && <span className="file-del-text">确认</span>}
    </button>
  )
}

/** 全案一键脱敏：对可读文档的解析文本原地打码（Core 侧幂等，原件二进制不动） */
function RedactAllButton({ caseId, readonly }: { caseId: string; readonly: boolean }): JSX.Element {
  const refreshDocuments = useApp((s) => s.refreshDocuments)
  const toast = useApp((s) => s.toast)
  const documents = useApp((s) => s.documents)
  const curFileId = useApp((s) => s.curFileId)
  const openDoc = useApp((s) => s.openDoc)
  const invalidatePages = useApp((s) => s.invalidatePages)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(0)

  // 可脱敏对象：本任务自有、未脱敏、解析完成、无待 OCR 页
  // （关联案件共享材料为只读，脱敏/删除等写操作只在来源案件内进行）
  const eligible = documents.filter(
    (d) =>
      (!d.caseId || d.caseId === caseId) &&
      !d.isRedacted &&
      (d.status === 'ready' || d.status === 'partial') &&
      d.ocrPending === 0
  )

  const onClick = async (): Promise<void> => {
    if (busy || readonly || eligible.length === 0) return
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    setProgress(0)
    let masked = 0
    let skipped = 0
    let failed = 0
    const redactedIds: string[] = []
    try {
      for (const d of eligible) {
        try {
          const res = await bridge.document.redactApply({ documentId: d.id })
          if (res.existing) skipped += 1
          else {
            masked += res.maskedCount
            redactedIds.push(d.id)
          }
        } catch {
          failed += 1 // 单份失败不中断全案流程（幂等，可再次点击补齐）
        }
        setProgress((p) => p + 1)
      }
      // 打码写入了页文本：丢弃缓存后刷新列表，重拉打码后的解析文本
      invalidatePages(redactedIds)
      await refreshDocuments()
      const parts = [`脱敏 ${redactedIds.length} 份（打码 ${masked} 处）`]
      if (skipped) parts.push(`已脱敏跳过 ${skipped} 份`)
      if (failed) parts.push(`失败 ${failed} 份（可重试）`)
      toast(`全案脱敏完成：${parts.join('，')}`)
      // 正在阅读的文档若刚脱敏，重进阅读以加载打码后文本
      if (curFileId && redactedIds.includes(curFileId)) openDoc(curFileId)
    } finally {
      setBusy(false)
      setProgress(0)
    }
  }

  return (
    <button
      className="btn-redact-all"
      disabled={readonly || busy || eligible.length === 0}
      title={
        readonly
          ? '案件已归档，无法脱敏'
          : eligible.length === 0
            ? '没有可脱敏的文档（需解析完成）'
            : `一键对全部 ${eligible.length} 份可读文档的解析文本自动脱敏（姓名/电话/邮箱/银行卡/住址等星号打码），原件文件不动，Agent 阅卷使用打码后文本`
      }
      onClick={() => void onClick()}
    >
      <Icon id="i-eye-off" />
      {busy ? `脱敏中 ${progress}/${eligible.length}` : '一键脱敏'}
    </button>
  )
}

/** 右侧面板：文档列表 ⇄ 阅读视图（互斥切换，设计规范 §5.4） */
export function RightPanel(): JSX.Element {
  const {
    rightW,
    rightHide,
    rightView,
    documents,
    curFileId,
    cases,
    currentCaseId,
    importing,
    importProgress,
    openDoc,
    openUnlock,
    importFiles,
    refreshDocuments,
    artOpen,
    artPinned,
    artExpanded,
    toggleArt,
    closeArt,
    toggleArtPin,
    toggleArtExpanded,
    loadArtifacts,
    toast
  } = useApp()
  const artifacts = useApp((s) => s.artifacts)
  const [kw, setKw] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [hits, setHits] = useState<SearchHit[]>([])
  const [searching, setSearching] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 批量选择模式：勾选多份文档后一键删除（替代逐份删除）
  const [selMode, setSelMode] = useState(false)
  const [sel, setSel] = useState<ReadonlySet<string>>(new Set())
  const [confirmBatch, setConfirmBatch] = useState(false)
  const deleteDocuments = useApp((s) => s.deleteDocuments)
  const exportBundle = useApp((s) => s.exportBundle)
  // 产物版本组（交付物迭代改写）：内联展开版本历史，可回退到任意历史版本
  const [verArtId, setVerArtId] = useState<string | null>(null)
  const [vers, setVers] = useState<ArtifactVersionItem[]>([])
  // 产物导出：概览内直接选格式导出（保存对话框由 Main 弹出，目标路径不进渲染进程）
  const [exportArtId, setExportArtId] = useState<string | null>(null)
  const exportArtifact = useApp((s) => s.exportArtifact)

  // 概览关闭后收起内联菜单，避免下次打开时残留展开态
  useEffect(() => {
    if (!artOpen) {
      setVerArtId(null)
      setExportArtId(null)
    }
  }, [artOpen])

  const toggleExportMenu = (artifactId: string): void => {
    setVerArtId(null)
    setExportArtId((prev) => (prev === artifactId ? null : artifactId))
  }

  const doExportArtifact = (artifactId: string, title: string, fmt: ArtifactExportFormat): void => {
    setExportArtId(null)
    void exportArtifact(artifactId, fmt, title)
  }

  const toggleVersions = async (artifactId: string): Promise<void> => {
    if (verArtId === artifactId) {
      setVerArtId(null)
      return
    }
    setExportArtId(null)
    const bridge = getBridge()
    if (!bridge) {
      toast('当前为浏览器调试模式，无法读取版本历史')
      return
    }
    try {
      const list = await bridge.artifact.listVersions(artifactId)
      setVers(list)
      setVerArtId(artifactId)
    } catch (e) {
      toast(`版本历史加载失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const restoreArtifactVersion = async (artifactId: string, versionNo: number): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      // 回退 = 复制历史版本追加为新当前版本（历史不删），打开新当前稿并刷新概览
      const cur = await bridge.artifact.restoreVersion({ artifactId, versionNo })
      await loadArtifacts()
      openDoc(cur.id)
      setVerArtId(null)
      toast(`已回退到 v${versionNo}（追加为当前版 v${cur.versionNo}，历史版本保留）`)
    } catch (e) {
      toast(`回退失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const current = cases.find((c) => c.id === currentCaseId) ?? null
  const readonly = !current || current.status === 'archived'
  const query = kw.trim()
  const filtered = documents.filter((f) => f.name.toLowerCase().includes(query.toLowerCase()))
  // 关联案件（文书生成只读共享）的材料：可阅读与检索，但删除/批量选择/脱敏仅限本任务自有材料
  const isShared = (d: DocMeta): boolean => !!currentCaseId && !!d.caseId && d.caseId !== currentCaseId
  const selectable = filtered.filter((f) => !isShared(f))
  const curDoc = documents.find((d) => d.id === curFileId)
  // 批量选择：全选/单选切换；列表刷新后剔除已不存在的文档，避免误报失败
  const allChecked = selectable.length > 0 && selectable.every((f) => sel.has(f.id))
  const someChecked = !allChecked && selectable.some((f) => sel.has(f.id))
  const toggleSel = (id: string): void => {
    const doc = documents.find((d) => d.id === id)
    if (!doc || isShared(doc)) return
    setSel((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleAll = (): void => setSel(allChecked ? new Set() : new Set(selectable.map((f) => f.id)))
  const exitSelMode = (): void => {
    setSelMode(false)
    setSel(new Set())
    setConfirmBatch(false)
  }
  // 两步确认（与单份删除一致）：首次点击进入确认态，3 秒未确认自动复位
  const onBatchDelete = async (): Promise<void> => {
    if (sel.size === 0) return
    if (!confirmBatch) {
      setConfirmBatch(true)
      setTimeout(() => setConfirmBatch(false), 3000)
      return
    }
    setConfirmBatch(false)
    const ids = [...sel]
    setSel(new Set())
    await deleteDocuments(ids)
  }
  useEffect(() => {
    // 全部删除后自动退出多选模式，操作栏不再显示
    if (documents.length === 0) {
      setSelMode(false)
      setSel(new Set())
      setConfirmBatch(false)
      return
    }
    setSel((prev) => {
      const ids = new Set(documents.map((d) => d.id))
      const next = [...prev].filter((id) => ids.has(id))
      return next.length === prev.size ? prev : new Set(next)
    })
  }, [documents])
  // 全文检索（300ms 防抖；案件强制过滤在服务端）
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current)
    if (!query || !currentCaseId || readonly) {
      setHits([])
      setSearching(false)
      return
    }
    setSearching(true)
    timer.current = setTimeout(() => {
      const bridge = getBridge()
      if (!bridge) {
        setHits([])
        setSearching(false)
        return
      }
      bridge.document
        .search({ caseId: currentCaseId, query, limit: 12 })
        .then(setHits)
        .catch(() => setHits([]))
        .finally(() => setSearching(false))
    }, 300)
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [query, currentCaseId, readonly])

  // 产物概览列表：默认最多 5 条，展开后全量
  const artList = artExpanded ? artifacts : artifacts.slice(0, 5)
  // 解析监控：存在待识别页或解析中状态时轮询进度；idle/paused 自动续跑（断点续跑，DEV-DOC-006）
  const parseKey = documents
    .filter((d) => d.ocrPending > 0 || INFLIGHT_STATUS.has(d.status))
    .map((d) => `${d.id}:${d.ocrPending}:${d.status}`)
    .join(',')
  // 每份文档最近一次「页进度实际变化」的时刻：只有持续不动才触发兜底补启动
  const lastProgressAt = useRef<Map<string, number>>(new Map())
  const lastPendingRef = useRef<Map<string, number>>(new Map())
  useEffect(() => {
    const now = Date.now()
    const seen = new Set<string>()
    for (const d of documents) {
      if (d.ocrPending <= 0) continue
      seen.add(d.id)
      // 待识别页数发生变化 = 有实际进展 → 重置计时
      if (lastPendingRef.current.get(d.id) !== d.ocrPending) {
        lastPendingRef.current.set(d.id, d.ocrPending)
        lastProgressAt.current.set(d.id, now)
      }
    }
    for (const id of [...lastProgressAt.current.keys()]) {
      if (!seen.has(id)) {
        lastProgressAt.current.delete(id)
        lastPendingRef.current.delete(id)
      }
    }
  }, [parseKey, documents])
  useEffect(() => {
    if (!parseKey) return
    const bridge = getBridge()
    if (!bridge) return
    let stopped = false
    const tick = async (): Promise<void> => {
      if (stopped) return
      let anyActive = false
      for (const d of useApp.getState().documents.filter((x) => x.ocrPending > 0 || INFLIGHT_STATUS.has(x.status))) {
        try {
          if (d.ocrPending > 0) {
            const st = await bridge.document.ocr.status(d.id)
            if (st.state === 'running') {
              anyActive = true
            } else if (st.state !== 'paused' && st.pending > 0) {
              // 解析属于 Core 的系统级能力：正常应由守护线程自动接手。这里仅在
              // 长时间无进展时补一次 start（幂等、立即返回），作为最后兜底，
              // 不再把「能否解析」押在渲染进程是否开着列表上。
              if (Date.now() - (lastProgressAt.current.get(d.id) ?? 0) >= OCR_STALL_MS) {
                lastProgressAt.current.set(d.id, Date.now())
                await bridge.document.ocr.start(d.id)
              }
              anyActive = true
            } else {
              // 用户暂停：保留 pending 页等待手动恢复；仅刷新状态
              anyActive = true
            }
          } else {
            anyActive = true // 解析中的文档：仅轮询刷新状态
          }
        } catch {
          /* 状态查询失败下次轮询重试 */
        }
      }
      if (anyActive) await refreshDocuments()
    }
    void tick()
    const timer2 = setInterval(() => void tick(), 2500)
    return () => {
      stopped = true
      clearInterval(timer2)
    }
  }, [parseKey, refreshDocuments])

  return (
    <section className={`rightpanel${rightHide ? ' hide' : ''}`} style={rightHide ? undefined : { width: rightW }}>
      {rightView === 'list' && (
        <div className="rp-view">
          <div className="rp-search">
            {searchOpen ? (
              // 搜索展开态：仅显示搜索框（其他按钮隐藏），可收起
              <div className="search-box">
                <Icon id="i-search" />
                <input
                  type="text"
                  placeholder="搜索文件名与全文..."
                  aria-label="搜索文档"
                  autoFocus
                  value={kw}
                  onChange={(e) => setKw(e.target.value)}
                />
                {searching && <span className="searching-dot" aria-label="检索中">·</span>}
                <button
                  className="icon-btn search-collapse"
                  aria-label="收起搜索"
                  title="收起搜索"
                  onClick={() => {
                    setSearchOpen(false)
                    setKw('')
                  }}
                >
                  <Icon id="i-x" />
                </button>
              </div>
            ) : (
              <>
                <button
                  className="icon-btn"
                  aria-label="搜索"
                  title="搜索文件名与全文"
                  onClick={() => setSearchOpen(true)}
                >
                  <Icon id="i-search" />
                </button>
                <button
                  className="btn-upload-doc"
                  disabled={readonly || importing}
                  onClick={() => void importFiles()}
                  title={readonly ? '案件归档或未选择，无法导入' : '导入文件'}
                >
                  <Icon id="i-plus" />
                  {importing
                    ? importProgress
                      ? `导入中 ${importProgress.done}/${importProgress.total}`
                      : '导入中…'
                    : '上传文档'}
                </button>
                <button
                  className="btn-folder-doc"
                  disabled={readonly || importing}
                  onClick={() => void importFiles('folder')}
                  title={
                    readonly ? '案件归档或未选择，无法导入' : '上传文件夹（自动过滤不支持的格式）'
                  }
                >
                  <Icon id="i-folder" />
                  上传文件夹
                </button>
                {current && currentCaseId && <RedactAllButton caseId={currentCaseId} readonly={readonly} />}
                <button
                  className={`icon-btn${selMode ? ' active' : ''}`}
                  aria-label={selMode ? '退出批量选择' : '批量选择文档'}
                  aria-pressed={selMode}
                  disabled={readonly || selectable.length === 0}
                  title={selMode ? '退出批量选择' : '批量选择文档（可多选删除）'}
                  onClick={() => (selMode ? exitSelMode() : setSelMode(true))}
                >
                  <Icon id="i-dots" />
                </button>
                <button
                  className={`icon-btn${artOpen ? ' active' : ''}`}
                  aria-label="文档概览"
                  title="产物概览（AI 生成文档）"
                  onClick={(e) => {
                    e.stopPropagation()
                    if (!artOpen) void loadArtifacts()
                    toggleArt()
                  }}
                >
                  <Icon id="i-list" />
                </button>
              </>
            )}
          </div>
          {artOpen && (
            <div className="art-pop" role="menu" aria-label="产物概览">
              <div className="art-head">
                <button className="mode" type="button" onClick={() => toast('切换概览分类（演示）')}>
                  概览
                  <Icon id="i-caret" className="icon caret" />
                </button>
                <button
                  className={`icon-btn${artPinned ? ' on' : ''}`}
                  id="artPin"
                  type="button"
                  aria-label="固定弹窗"
                  title="固定弹窗"
                  aria-pressed={artPinned}
                  onClick={toggleArtPin}
                >
                  <Icon id="i-pin" />
                </button>
              </div>
              <div className="art-sec">
                产物
                <Icon id="i-caret" className="icon caret" />
              </div>
              <div className="art-list">
                {artList.length === 0 ? (
                  <div className="art-empty" style={{ padding: '16px 12px', color: 'var(--ink-300)' }}>
                    暂无 AI 产物，发起阅卷或文书起草任务后生成
                  </div>
                ) : (
                  artList.map((f) => {
                    const ext = f.title.includes('.') ? f.title.slice(f.title.lastIndexOf('.') + 1).toLowerCase() : 'md'
                    const letter = ext === 'docx' ? 'W' : ext === 'pdf' ? 'P' : ext === 'xlsx' ? 'X' : ext === 'pptx' ? 'S' : ext === 'md' ? 'M' : (ext[0] || 'F').toUpperCase()
                    // 所有产物（含文书生成 Word）统一走右侧文档预览：
                    // 原件按真实格式渲染，编辑能力由 ReadingView 的 ArtifactEditor 提供
                    const open = () => openDoc(f.id)
                    return (
                    <div key={f.id} className={`art-item${f.id === curFileId ? ' on' : ''}${verArtId === f.id ? ' art-item-ver-open' : ''}`}>
                      <div
                        className="art-item-main"
                        role="menuitem"
                        tabIndex={0}
                        title={f.title}
                        onClick={() => {
                          open()
                          closeArt()
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            open()
                            closeArt()
                          }
                        }}
                      >
                        <span className={`ftype ${ext}${ext.length === 1 ? ' file' : ''}`}>{letter}</span>
                        <span className="art-item-title">{f.title}</span>
                        {f.versionCount > 1 && (
                          <button
                            className="art-ver-badge"
                            type="button"
                            title={`版本组：共 ${f.versionCount} 版，当前 v${f.versionNo}；点击查看版本历史`}
                            onClick={(e) => {
                              e.stopPropagation()
                              void toggleVersions(f.id)
                            }}
                          >
                            v{f.versionNo}/{f.versionCount}
                          </button>
                        )}
                        <button
                          className={`art-export-trigger${exportArtId === f.id ? ' on' : ''}`}
                          type="button"
                          aria-label={`导出产物 ${f.title}`}
                          aria-expanded={exportArtId === f.id}
                          title="导出产物（选择格式）"
                          onClick={(e) => {
                            e.stopPropagation()
                            toggleExportMenu(f.id)
                          }}
                        >
                          <Icon id="i-download" />
                        </button>
                        <DismissArtButton artifactId={f.id} artName={f.title} readonly={readonly} />
                      </div>
                      {verArtId === f.id && (
                        <div className="art-vers" role="menu" aria-label="版本历史">
                          {vers.map((v) => (
                            <div
                              key={v.id}
                              className={`art-ver${v.versionNo === f.versionNo ? ' cur' : ''}`}
                              title={`v${v.versionNo} · ${(v.createdAt || '').slice(0, 16).replace('T', ' ')}`}
                            >
                              <span className="art-ver-label">
                                v{v.versionNo}
                                {v.versionNo === f.versionNo ? '（当前）' : ''}
                              </span>
                              <span className="art-ver-time">{(v.createdAt || '').slice(0, 16).replace('T', ' ')}</span>
                              {v.versionNo !== f.versionNo && (
                                <button
                                  type="button"
                                  className="art-ver-restore"
                                  title="回退到该版本（复制为新当前版本，历史保留）"
                                  onClick={() => void restoreArtifactVersion(f.id, v.versionNo)}
                                >
                                  回退
                                </button>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                      {exportArtId === f.id && (
                        <div className="art-export" role="menu" aria-label={`导出 ${f.title}`}>
                          {exportOptionsFor(f).map(({ fmt, label, hint }) => (
                            <button
                              key={fmt}
                              type="button"
                              className="art-export-fmt"
                              title={`${hint}（弹出保存对话框）`}
                              onClick={(e) => {
                                e.stopPropagation()
                                doExportArtifact(f.id, f.title, fmt as ArtifactExportFormat)
                              }}
                            >
                              {label}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    )
                  })
                )}
              </div>
              {artifacts.length > 5 && (
                <button className="art-more" type="button" onClick={toggleArtExpanded}>
                  {artExpanded ? '收起' : `展开更多（${artifacts.length - 5}）`}
                </button>
              )}
            </div>
          )}
          <div className="rp-caption">
            {selMode ? (
              <div className="sel-bar">
                <button
                  className={`file-check${allChecked ? ' on' : ''}${someChecked ? ' some' : ''}`}
                  role="checkbox"
                  aria-checked={someChecked ? 'mixed' : allChecked}
                  aria-label="全选文档"
                  title={allChecked ? '取消全选' : '全选当前列表文档'}
                  onClick={toggleAll}
                >
                  <Icon id={someChecked ? 'i-minus' : 'i-check'} />
                </button>
                <span className="sel-bar-label">全选</span>
                <span className="sel-count">已选 {sel.size} / {filtered.length} 份</span>
                <button
                  className="btn-bundle-sel"
                  disabled={sel.size === 0}
                  title="将所选材料合并为带封面、目录、连续页码与书签的 PDF（原件只读，非 PDF 材料会转换后装入）"
                  onClick={() => void exportBundle([...sel])}
                >
                  装订证据卷
                </button>
                <button
                  className={`btn-del-sel${confirmBatch ? ' confirm' : ''}`}
                  disabled={sel.size === 0}
                  title={
                    confirmBatch
                      ? `再次点击确认删除所选 ${sel.size} 份文档（相关结论将标记失效）`
                      : '删除所选文档（相关结论将标记失效）'
                  }
                  onClick={() => void onBatchDelete()}
                >
                  {confirmBatch ? `确认删除 ${sel.size} 份？` : '删除所选'}
                </button>
                <button className="sel-cancel" onClick={exitSelMode}>
                  取消
                </button>
              </div>
            ) : (
              <>
                <b>文档列表</b>
                <button
                  className="rp-reading"
                  type="button"
                  title="继续阅读"
                  onClick={() => curFileId && openDoc(curFileId)}
                >
                  正在阅读: <span>{curDoc ? curDoc.name : '——'}</span>
                  <Icon id="i-chev2" />
                </button>
              </>
            )}
          </div>
          <div className="file-list" role="listbox" aria-label="案件文档列表">
            {!current ? (
              <div className="file-empty">请先选择或新建案件</div>
            ) : readonly ? (
              <div className="file-empty">
                案件已归档（只读）
                <br />
                恢复案件后可继续导入
              </div>
            ) : (
              <>
                {hits.length > 0 && (
                  <>
                    <div className="list-group-title">
                      全文命中 <span>{hits.length}</span>
                    </div>
                    {hits.map((h) => (
                      <button
                        key={`${h.documentId}-${h.pageNo}`}
                        className="file-item hit-item"
                        role="option"
                        aria-selected={false}
                        title={`${h.documentTitle}${h.pageNo > 1 ? ` · 第${h.pageNo}页` : ''}`}
                        onClick={() => openDoc(h.documentId)}
                      >
                        <span className="hit-body">
                          <span className="hit-title">
                            {h.documentTitle}
                            {h.pageNo > 1 ? ` · 第${h.pageNo}页` : ''}
                            {h.matchType === 'like' && <em className="hit-type">短词</em>}
                          </span>
                          <span className="hit-snippet">
                            <Snippet text={h.snippet} />
                          </span>
                        </span>
                      </button>
                    ))}
                  </>
                )}
                {hits.length === 0 && query && !searching && documents.length > 0 && filtered.length === 0 && (
                  <div className="file-empty">未找到匹配的文档或全文内容</div>
                )}
                {documents.length > 0 && (
                  <div className="list-group-title">
                    案件文件 <span>{filtered.length}</span>
                  </div>
                )}
                {documents.length === 0 ? (
                  <div className="file-empty">暂无文档，点击「上传文档」导入卷宗</div>
                ) : (
                  filtered.map((f) => {
                    const shared = isShared(f)
                    return (
                    <div
                      key={f.id}
                      className={`file-item${f.id === curFileId ? ' sel' : ''}${selMode ? ' selecting' : ''}`}
                      role="option"
                      aria-selected={selMode ? sel.has(f.id) : f.id === curFileId}
                      tabIndex={0}
                      onClick={() => {
                        if (selMode && !shared) {
                          toggleSel(f.id)
                          return
                        }
                        // 加密文档先解锁再阅读（REQ-DOC-004）
                        if (f.status === 'need_password') openUnlock(f.id, f.name)
                        else openDoc(f.id)
                      }}
                      onKeyDown={(e) => {
                        // DEV-UI-008：键盘可完成主流程（Enter/空格打开；批量模式下切换勾选）
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          if (selMode && !shared) toggleSel(f.id)
                          else if (f.status === 'need_password') openUnlock(f.id, f.name)
                          else openDoc(f.id)
                        }
                      }}
                    >
                      {selMode && !shared && (
                        <span className={`file-check${sel.has(f.id) ? ' on' : ''}`} aria-hidden="true">
                          <Icon id="i-check" />
                        </span>
                      )}
                      <span className={`ftype ${f.type}`}>{badge(f.type)}</span>
                      <span className="file-name" title={f.name}>{f.name}</span>
                      {shared && (
                        <span
                          className="file-redacted-badge"
                          title="来自关联案件（只读共享）：可用于文书生成与检索，原件与归属不变"
                        >
                          <Icon id="i-link" />
                          关联
                        </span>
                      )}
                      {f.isRedacted && (
                        <span className="file-redacted-badge" title="已脱敏：文本视图黄底显示打码位置，原件预览显示原始内容">
                          <Icon id="i-eye-off" />
                          已脱敏
                        </span>
                      )}
                      {(() => {
                        const sb = statusBadge(f)
                        return (
                          <span className={`file-status${sb.cls}`} title={sb.tip}>
                            {sb.text}
                          </span>
                        )
                      })()}
                      <span className="file-date">{f.date}</span>
                      {!selMode && !shared && <DelDocButton docId={f.id} docName={f.name} />}
                    </div>
                    )
                  })
                )}
              </>
            )}
          </div>
        </div>
      )}

      {rightView === 'docgen' && <DocGenWorkbench />}
      {rightView === 'read' && <ReadingView />}
    </section>
  )
}
