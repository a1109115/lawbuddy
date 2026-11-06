import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import type { ArtifactExportFormat, DocumentTextBlock } from '@lawbuddy/contracts'
import { badge, useApp } from '../state/store'
import { getBridge } from '../services/bridge'
import { exportOptionsFor, graphicOptions, supportsGraphicExport } from '../services/exportFormats'
import { findGraphicSvg, serializeSvg, svgToPngBase64 } from '../services/graphicExport'
import { BodyView } from './BodyView'
import { Icon } from './Icon'
import { ArtifactEditor } from './preview/ArtifactEditor'
import { OriginalPreview } from './preview/OriginalPreview'
import { PageFrame, TextDocument } from './preview/PageFrame'
import { supportsOriginalPreview } from '../services/preview'

/** 可直接编辑的 AI 生成文档格式（案件卷宗原件始终只读） */
const EDITABLE_ARTIFACT_KINDS = new Set([
  'docx', 'md', 'markdown', 'txt', 'html', 'htm', 'xlsx', 'pptx'
])

/** 引文定位规范化：与 Core Q-010 一致，忽略空白、断行与全角空格 */
function normalizeForLocate(text: string): string {
  return text.replace(/[\s\u3000]+/g, '')
}

/** 引文近似定位候选：按标点/省略号切段，长段优先（仅用于定位，不代表证据已支持） */
function locatorSegments(quote: string): string[] {
  return Array.from(new Set(
    quote.split(/[。；;，,、：:！!？?…⋯]+|\.{3,}|\n+/)
      .map(normalizeForLocate)
      .filter((part) => part.length >= 6)
  )).sort((a, b) => b.length - a.length)
}

/** 页内正文文本节点 → 去掉空白的 haystack 与「haystack 下标 → DOM 文本位置」映射 */
function buildLocateIndex(host: HTMLElement): {
  haystack: string
  positions: Array<{ node: Text; offset: number }>
} {
  const positions: Array<{ node: Text; offset: number }> = []
  let haystack = ''
  const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = (node as Text).parentElement
      if (!parent || !node.nodeValue) return NodeFilter.FILTER_REJECT
      if (parent.closest('.ann-pop,.ann-peek,.sel-tool,.page-badge,.ocr-fix-bar')) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    }
  })
  let current: Node | null
  while ((current = walker.nextNode())) {
    const node = current as Text
    for (let offset = 0; offset < node.data.length; offset++) {
      if (/\s/.test(node.data[offset])) continue
      haystack += node.data[offset]
      positions.push({ node, offset })
    }
  }
  return { haystack, positions }
}

function rangeFromPositions(
  positions: Array<{ node: Text; offset: number }>,
  start: number,
  end: number
): Range | null {
  const first = positions[start]
  const last = positions[end - 1]
  if (!first || !last) return null
  const range = document.createRange()
  range.setStart(first.node, first.offset)
  range.setEnd(last.node, last.offset + 1)
  return range
}

interface LocateRect {
  left: number
  top: number
  width: number
  height: number
}

/** OCR 行：规范化文本 + 该行在整页 haystack 中的区间 + 页面归一化 bbox */
interface OcrLine {
  text: string
  bbox: [number, number, number, number]
  start: number
  end: number
}

/** 把 OCR 行按阅读顺序拼成去空白 haystack，并保留每行到页面的坐标 */
function buildBlockHaystack(blocks: DocumentTextBlock[]): { haystack: string; lines: OcrLine[] } {
  let haystack = ''
  const lines: OcrLine[] = []
  for (const block of blocks) {
    if (!block.bbox || !block.text) continue
    const text = normalizeForLocate(block.text)
    if (!text) continue
    const start = haystack.length
    haystack += text
    lines.push({ text, bbox: block.bbox, start, end: haystack.length })
  }
  return { haystack, lines }
}

/**
 * OCR 行坐标 → 原件上的视口矩形。
 * bbox 是页面宽高归一化值；跨行引文按行拆分，行内片段按字符比例截取横向范围。
 */
function ocrHighlightRects(
  page: HTMLElement,
  lines: OcrLine[],
  start: number,
  end: number
): LocateRect[] {
  const target = page.querySelector<HTMLElement>('.pdf-page')
    ?? page.querySelector<HTMLElement>('.doc-image')
    ?? page
  const box = target.getBoundingClientRect()
  const originX = box.left + target.clientLeft
  const originY = box.top + target.clientTop
  const width = target.clientWidth || box.width
  const height = target.clientHeight || box.height
  const rects: LocateRect[] = []
  for (const line of lines) {
    const from = Math.max(start, line.start)
    const to = Math.min(end, line.end)
    if (to <= from) continue
    const span = Math.max(1, line.text.length)
    const x1 = line.bbox[0] + (line.bbox[2] - line.bbox[0]) * ((from - line.start) / span)
    const x2 = line.bbox[0] + (line.bbox[2] - line.bbox[0]) * ((to - line.start) / span)
    rects.push({
      left: originX + x1 * width,
      top: originY + line.bbox[1] * height,
      width: Math.max(2, (x2 - x1) * width),
      height: Math.max(2, (line.bbox[3] - line.bbox[1]) * height)
    })
  }
  return rects
}

/**
 * 阅读视图（设计规范 §5.4 / §6）：
 * 标签栏 + 原件预览/文本视图 + 悬浮工具栏 + 引证定位高亮。
 * 真实卷宗默认按**原件真实格式**渲染（PDF/Word/Excel/Markdown/HTML/图片，REQ-DOC-014），
 * 可切换为 Core 提取的页文本；AI 产物按结构化 body 渲染（可导出 MD/DOCX）。
 */
export function ReadingView(): JSX.Element {
  const {
    getRenderDoc,
    docPages,
    docPageMeta,
    curFileId,
    openTabs,
    openDoc,
    closeTab,
    showList,
    backToList,
    zoom,
    setZoom,
    previewMode,
    setPreviewMode,
    fullscreen,
    setFullscreen,
    docSearchOpen,
    docSearchTerm,
    setDocSearch,
    citeTarget,
    toggleRight,
    openDocGen
  } = useApp()
  const currentCaseId = useApp((s) => s.currentCaseId)
  const openCitation = useApp((s) => s.openCitation)
  const openClaimsQueue = useApp((s) => s.openClaimsQueue)
  const exportArtifact = useApp((s) => s.exportArtifact)
  const [exportOpen, setExportOpen] = useState(false)
  // AI 生成文档的直接编辑（无工作副本）；案件卷宗原件不进入编辑
  const [editing, setEditing] = useState(false)
  /** 导入 Excel/PPT 时，编辑对象是新建的可编辑副本（原件只读） */
  const [editArtifactId, setEditArtifactId] = useState<string | null>(null)
  const [copyBusy, setCopyBusy] = useState(false)
  /** 编辑中拦截离开阅读视图的操作：先点「完成」保存，避免编辑内容丢失 */
  const guardEditing = (): boolean => {
    if (!editing) return true
    useApp.getState().toast('正在编辑文档，请先点击「完成」保存后离开')
    return false
  }

  const scrollRef = useRef<HTMLDivElement>(null)
  const paperRef = useRef<HTMLDivElement>(null)
  const [pageInput, setPageInput] = useState('1')
  const [preview, setPreview] = useState<{ id: string; x: number; y: number } | null>(null)
  const previewTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const doc = curFileId ? getRenderDoc(curFileId) : undefined
  // 真实卷宗：页文本异步加载完成前显示加载态
  const isRealDoc = !!doc && !doc.isArtifact
  // 文件类产物（AI 生成的 Word/Excel/PPT 等）：无提取文本，原件预览是唯一内容来源
  const isFileArtifact = !!doc?.isFileArtifact
  const loadingPages = isRealDoc && !docPages[doc.id]
  // 原件预览可用性（未支持类型的原件无法解码；文件类产物回落到说明文案）
  const canRenderOriginal = !!doc && supportsOriginalPreview(doc.kind)
  // HTML 没有有意义的提取文本：始终按原件渲染（沙箱 iframe 展示页面）
  const isHtmlDoc = doc?.kind === 'html' || doc?.kind === 'htm'
  // 已脱敏文档：解析文本已原地打码（文本视图黄底高亮）；原件预览仍渲染原始文件
  const isRedacted = !!doc?.isRedacted
  // 文件类产物没有「提取文本」视图，支持原件时强制按原件渲染；HTML 同理
  const viewMode =
    (isFileArtifact || isHtmlDoc) && canRenderOriginal
      ? 'render'
      : canRenderOriginal
        ? previewMode
        : 'text'
  // 可直接编辑：AI 生成的文档（文件类产物）+ 当前任务可写；
  // 文书生成落盘的 Word 同样复用此编辑器（只改导出文件，不回写草稿正文）
  const caseActive = useApp((s) => {
    const c = [...s.cases, ...s.archivedCases].find((x) => x.id === s.currentCaseId)
    return c?.status === 'active'
  })
  const editableDoc =
    !!doc &&
    !!doc.isFileArtifact &&
    EDITABLE_ARTIFACT_KINDS.has(doc.kind.toLowerCase()) &&
    caseActive
  // 导入的 Excel/PPT 原件：不直接改原件，先创建可编辑副本（副本登记为文件产物）
  const copyEditableDoc =
    !!doc &&
    !doc.isFileArtifact &&
    (doc.kind.toLowerCase() === 'xlsx' || doc.kind.toLowerCase() === 'pptx') &&
    caseActive
  const beginCopyEdit = async (): Promise<void> => {
    if (!doc || copyBusy) return
    const bridge = getBridge()
    if (!bridge) {
      useApp.getState().toast('本地服务未连接，无法创建可编辑副本')
      return
    }
    setCopyBusy(true)
    try {
      const copy = await bridge.document.createEditableCopy({ documentId: doc.id })
      setEditArtifactId(copy.artifactId)
      setEditing(true)
      void useApp.getState().loadArtifacts()
      useApp.getState().toast(`已创建可编辑副本「${copy.title}」，原件保持不变`)
    } catch (e) {
      useApp.getState().toast(`创建可编辑副本失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setCopyBusy(false)
    }
  }
  const docgenArtifact = !!doc?.docgenDraftId
  const editTitle = docgenArtifact
    ? '编辑导出的 Word 文件（保存写回文件，不自动回写文书草稿正文）'
    : '直接编辑 AI 生成文档（保存写回原文件）'
  // 产物导出：选项由产物类型与源格式决定（原格式复制 / 原生转换 / 数据 / 图形）
  const doExport = (fmt: ArtifactExportFormat): void => {
    if (!doc) return
    setExportOpen(false)
    void exportArtifact(doc.id, fmt, doc.name)
  }
  /** 可视化产物图形导出：序列化页面上真实渲染的 SVG，PNG 由 canvas 栅格化 */
  const doGraphicExport = async (fmt: 'svg' | 'png'): Promise<void> => {
    if (!doc) return
    setExportOpen(false)
    const bridge = getBridge()
    if (!bridge) return
    const svg = findGraphicSvg(paperRef.current)
    if (!svg) {
      useApp.getState().toast('当前视图没有可导出的图形，请先切换到关系图或流程图')
      return
    }
    try {
      const text = serializeSvg(svg)
      const dataBase64 = fmt === 'png' ? await svgToPngBase64(text) : undefined
      const r = await bridge.artifact.saveGraphic({
        artifactId: doc.id,
        title: doc.name,
        format: fmt,
        svg: fmt === 'svg' ? text : undefined,
        dataBase64
      })
      if (!r) {
        useApp.getState().toast('已取消导出')
        return
      }
      useApp.getState().toast(`已导出图形 ${r.fileName}（${r.sizeBytes} 字节）`)
    } catch (e) {
      useApp.getState().toast(`图形导出失败：${e instanceof Error ? e.message : String(e)}`)
    }
  }
  const artifactIsViz = !!doc?.isArtifact && supportsGraphicExport({ kind: '', hasVizBody: !!doc?.body?.viz })
  // 产物元数据（kind 决定导出能力；结构化产物与文件类产物选项不同）
  const artMeta = useApp((s) => s.artifacts.find((a) => a.id === doc?.id))
  // 统计本篇打码处数（连续星号串计 1 处），用于脱敏提示条
  const maskedCount = useMemo(() => {
    if (!isRedacted || !doc) return 0
    return (docPages[doc.id] ?? []).reduce((n, t) => n + (t.match(/\*{2,}/g)?.length ?? 0), 0)
  }, [isRedacted, doc, docPages])
  const ensureAnnotations = useApp((s) => s.ensureAnnotations)

  // 页批注（DEV-DOC-010）：卷宗与 AI 产物共用同一批注数据（产物走 artifact 通道）
  useEffect(() => {
    if (curFileId) void ensureAnnotations(curFileId)
  }, [curFileId, ensureAnnotations])

  // 切换文档时退出编辑态（编辑中切换由 store 的未保存守卫拦截）
  useEffect(() => {
    setEditing(false)
    setEditArtifactId(null)
  }, [curFileId])

  // —— 每页外壳：页码标识 + OCR 人工更正 + 页批注；原件预览与页文本共用 ——
  // 关联案件（文书生成只读共享）的材料：可阅读与引用，但 OCR 更正/批注只在来源案件维护
  const sharedDoc = !!doc?.caseId && !!currentCaseId && doc.caseId !== currentCaseId
  const docId = doc?.id
  const docKind = doc?.kind
  const docPagesCount = Math.max(1, doc?.pages || 1)
  const renderFrame = useCallback(
    (pageNo: number, content: ReactNode) =>
      docId ? (
        <PageFrame
          docId={docId}
          pageNo={pageNo}
          totalPages={docPagesCount}
          text={(docPages[docId] ?? [])[pageNo - 1] ?? ''}
          meta={(docPageMeta[docId] ?? [])[pageNo - 1]}
          readOnly={sharedDoc}
          // 沙箱 iframe（HTML）内无法选中文本：批注改为手工粘贴锚定原文
          allowQuoteSelection={docKind !== 'html' && docKind !== 'htm'}
        >
          {content}
        </PageFrame>
      ) : null,
    [docId, docKind, docPagesCount, docPages, docPageMeta, sharedDoc]
  )

  // —— Esc 层级（设计规范 §7）：关闭浮层 → 关闭搜索 → 退出全屏（DEV-UI-008） ——
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape') return
      if (exportOpen) {
        setExportOpen(false)
        return
      }
      if (preview) {
        clearPreview()
        return
      }
      if (docSearchOpen) {
        setDocSearch(false)
        return
      }
      if (fullscreen) setFullscreen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // —— 引证定位（设计规范 §6）：直接在原件里定位。PDF 使用 pdf.js 文本层，
  // 兼容懒渲染（页/文本层异步出现）、页码偏差与引文省略，尽量减少定位失败。 ——
  useEffect(() => {
    const paper = paperRef.current
    const scrollRoot = scrollRef.current
    if (!paper || !scrollRoot || !citeTarget || citeTarget.docId !== curFileId) return
    const target = citeTarget
    const quote = target.quote?.trim() ?? ''
    let disposed = false
    let settled = false
    let scrolled = false
    let observer: MutationObserver | null = null
    let raf = 0
    let timer = 0
    let notified = false
    const startedAt = Date.now()
    const deadline = startedAt + 20000
    // 扫描件 PDF 可能没有文本层：等待一段时间后按页码定位，不无限等待
    const textDeadline = Date.now() + 5000

    const clearHighlights = (): void => {
      paper.querySelectorAll('.cite-highlight-layer').forEach((el) => el.remove())
    }

    /** 先用 Core 页文本核对页码：模型页码可能偏差，文本层尚未渲染时也能选对页 */
    const resolvePageNo = (): number => {
      const texts = docPages[target.docId] ?? []
      if (!quote || !texts.length) return target.pageNo
      const normQuote = normalizeForLocate(quote)
      const hits = texts.reduce<number[]>((acc, text, index) => {
        if (normalizeForLocate(text).includes(normQuote)) acc.push(index + 1)
        return acc
      }, [])
      if (!hits.length || hits.includes(target.pageNo)) return target.pageNo
      return hits[0]
    }
    // docPages 变化会重建 effect，这里只解析一次，避免等待 PDF 渲染期间反复扫描全文
    const resolvedPageNo = resolvePageNo()

    /** 在页容器上叠加定位框（原件与文本视图共用，不遮挡文字区域的选择与批注） */
    const drawHighlight = (page: HTMLElement, rects: LocateRect[]): HTMLElement | null => {
      const hostRect = page.getBoundingClientRect()
      const layer = document.createElement('div')
      layer.className = 'cite-highlight-layer'
      let firstBox: HTMLElement | null = null
      for (const rect of rects.slice(0, 120)) {
        if (rect.width < 2 || rect.height < 2) continue
        const box = document.createElement('div')
        box.className = 'cite-highlight-box'
        box.style.left = `${rect.left - hostRect.left - page.clientLeft}px`
        box.style.top = `${rect.top - hostRect.top - page.clientTop}px`
        box.style.width = `${rect.width}px`
        box.style.height = `${rect.height}px`
        layer.appendChild(box)
        firstBox ??= box
      }
      if (!firstBox) return null
      page.appendChild(layer)
      return firstBox
    }

    const rangeRects = (range: Range): LocateRect[] =>
      Array.from(range.getClientRects()).map((r) => ({
        left: r.left, top: r.top, width: r.width, height: r.height
      }))

    /** 在去空白 haystack 中找引文：先精确匹配，再按标点长段近似匹配 */
    const findMatch = (haystack: string): { start: number; end: number; exact: boolean } | null => {
      const normQuote = normalizeForLocate(quote)
      if (!normQuote) return null
      const exactStart = haystack.indexOf(normQuote)
      if (exactStart >= 0) {
        return { start: exactStart, end: exactStart + normQuote.length, exact: true }
      }
      for (const segment of locatorSegments(quote)) {
        const i = haystack.indexOf(segment)
        if (i >= 0) return { start: i, end: i + segment.length, exact: false }
      }
      return null
    }

    /** 扫描件 PDF / 图片没有文本层：按需拉取 Core OCR 行（含页面归一化 bbox） */
    let blocks: DocumentTextBlock[] | null = null
    let blocksLoading = false
    const loadBlocks = (): DocumentTextBlock[] | null => {
      if (blocks !== null) return blocks
      if (blocksLoading) return null
      blocksLoading = true
      const bridge = getBridge()
      if (!bridge) {
        blocks = []
        return blocks
      }
      void bridge.document.getTextBlocks(target.docId, resolvedPageNo)
        .then((rows) => { blocks = Array.isArray(rows) ? rows : [] })
        .catch(() => { blocks = [] })
      return null
    }

    const scrollToBox = (box: HTMLElement | null): void => {
      if (!box) return
      const rootRect = scrollRoot.getBoundingClientRect()
      const boxRect = box.getBoundingClientRect()
      scrollRoot.scrollTo({
        top: scrollRoot.scrollTop + (boxRect.top - rootRect.top) - rootRect.height / 2 + boxRect.height / 2,
        behavior: 'smooth'
      })
    }

    const attempt = (): boolean => {
      if (disposed) return true
      const pageNo = resolvedPageNo
      const pageNodes = paper.querySelectorAll<HTMLElement>('.doc-page')
      // Word/PPT/文本类原件预览不分页：整篇只有一个页容器，按页码查找会落空
      const singlePage = pageNodes.length === 1
      const page = paper.querySelector<HTMLElement>(`.doc-page[data-page="${pageNo}"]`)
        ?? (singlePage ? pageNodes[0] : null)
      if (!page) return false
      if (!scrolled) {
        // 先让目标页进入视口，PDF/Word 的懒渲染才会产出文本层
        page.scrollIntoView({ block: 'center', behavior: 'auto' })
        scrolled = true
      }
      if (!quote) {
        clearHighlights()
        settled = true
        return true
      }

      const body = page.querySelector<HTMLElement>('.doc-page-body') ?? page
      const hasTextLayer = !!(body.textContent ?? '').trim()
      let hit: { rects: LocateRect[]; exact: boolean } | null = null

      if (hasTextLayer) {
        const { haystack, positions } = buildLocateIndex(body)
        const match = haystack ? findMatch(haystack) : null
        const range = match ? rangeFromPositions(positions, match.start, match.end) : null
        if (match && range) hit = { rects: rangeRects(range), exact: match.exact }
      }

      if (!hit) {
        // 扫描件 PDF/图片：文本层缺失或未命中时，用 OCR 行坐标做片段级定位
        const ocrBlocks = loadBlocks()
        if (ocrBlocks === null) return false // 等待 OCR 行加载
        if (ocrBlocks.length) {
          const { haystack, lines } = buildBlockHaystack(ocrBlocks)
          const match = haystack ? findMatch(haystack) : null
          if (match) {
            const rects = ocrHighlightRects(page, lines, match.start, match.end)
            if (rects.length) hit = { rects, exact: match.exact }
          }
        }
      }

      clearHighlights()
      if (!hit) {
        // 扫描件 OCR 可能仍在写入：短暂等待后再降级为页级定位
        if (!hasTextLayer && Date.now() <= textDeadline) return false
        useApp.getState().toast(`第 ${pageNo} 页未匹配到可高亮的原文片段，已定位到该页，请人工核对`)
        settled = true
        return true
      }
      const box = drawHighlight(page, hit.rects)
      scrollToBox(box)
      if (!hit.exact) {
        useApp.getState().toast('引文与原文存在差异，已按可匹配片段近似定位，请核对')
      } else if (!singlePage && pageNo !== target.pageNo) {
        useApp.getState().toast(`引文实际位于第 ${pageNo} 页`)
      }
      settled = true
      return true
    }

    const tick = (): void => {
      if (disposed || settled) return
      if (attempt()) {
        observer?.disconnect()
        window.clearInterval(timer)
        return
      }
      if (!notified && Date.now() - startedAt > 6000) {
        notified = true
        useApp.getState().toast('正在加载原件页面，完成后会自动定位到原文')
      }
      if (Date.now() > deadline) {
        observer?.disconnect()
        window.clearInterval(timer)
        useApp.getState().toast('文档仍在渲染，暂时无法定位到原文，请稍后重试')
      }
    }

    observer = new MutationObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(tick)
    })
    observer.observe(paper, { childList: true, subtree: true, characterData: true })
    timer = window.setInterval(tick, 300)
    tick()

    return () => {
      disposed = true
      observer?.disconnect()
      window.clearInterval(timer)
      cancelAnimationFrame(raf)
      clearHighlights()
    }
  }, [citeTarget, curFileId, docPages, previewMode, zoom])

  // —— 文档内搜索高亮（#FFE58A） ——
  const applyMarks = useCallback((term: string): void => {
    const paper = paperRef.current
    if (!paper) return
    paper.querySelectorAll('mark[data-s]').forEach((m) => m.replaceWith(document.createTextNode(m.textContent ?? '')))
    paper.normalize()
    if (!term) return
    const walker = document.createTreeWalker(paper, NodeFilter.SHOW_TEXT)
    const nodes: Text[] = []
    while (walker.nextNode()) nodes.push(walker.currentNode as Text)
    let first: HTMLElement | null = null
    for (const n of nodes) {
      if (n.parentNode?.nodeName === 'MARK') continue
      const i = n.textContent.indexOf(term)
      if (i < 0) continue
      const after = n.splitText(i)
      if (after.textContent.length > term.length) after.splitText(term.length)
      const m = document.createElement('mark')
      m.dataset.s = '1'
      m.textContent = term
      after.replaceWith(m)
      if (!first) first = m
    }
    first?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [])

  useEffect(() => {
    if (docSearchOpen) applyMarks(docSearchTerm.trim())
    else applyMarks('')
  }, [docSearchOpen, docSearchTerm, curFileId, docPages, applyMarks])

  // —— 页码跳转：定位到对应页容器（原件预览与文本视图均按 data-page 分页） ——
  const jumpPage = (raw: string): void => {
    if (!doc) return
    const p = Math.max(1, Math.min(doc.pages || 1, Number.parseInt(raw, 10) || 1))
    setPageInput(String(p))
    const target = paperRef.current?.querySelector(`.doc-page[data-page="${p}"]`) as HTMLElement | null
    target?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }

  // —— 标签悬停预览小窗（380ms 延迟） ——
  const onTabHover = (id: string, target: HTMLElement): void => {
    if (previewTimer.current) clearTimeout(previewTimer.current)
    previewTimer.current = setTimeout(() => {
      const r = target.getBoundingClientRect()
      setPreview({ id, x: Math.min(r.left, window.innerWidth - 270), y: r.bottom + 8 })
    }, 380)
  }
  const clearPreview = (): void => {
    if (previewTimer.current) clearTimeout(previewTimer.current)
    setPreview(null)
  }

  const previewDoc = preview ? getRenderDoc(preview.id) : undefined

  return (
    <div className={`rp-view pv-view${fullscreen ? ' fullscreen' : ''}`}>
      <div className="tabbar">
        <button className="icon-btn" aria-label="返回文档列表" title="返回文档列表" onClick={() => guardEditing() && backToList()}>
          <Icon id="i-chevl" />
        </button>
        <div className="tabs" role="tablist" aria-label="已打开文档">
          {openTabs.map((id) => {
            const f = getRenderDoc(id)
            if (!f) return null
            return (
              <div
                key={id}
                className={`tab${id === curFileId ? ' on' : ''}`}
                role="tab"
                aria-selected={id === curFileId}
                tabIndex={0}
                title={f.name}
                onClick={() => id !== curFileId && guardEditing() && openDoc(id)}
                onKeyDown={(e) => {
                  // DEV-UI-008：键盘可切换文档标签
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    if (id !== curFileId) openDoc(id)
                  }
                }}
                onMouseEnter={(e) => onTabHover(id, e.currentTarget)}
                onMouseLeave={clearPreview}
              >
                <span className={`ftype ${f.type}`}>{badge(f.type)}</span>
                <span className="tab-name">{f.name}</span>
                <button
                  className="tab-x"
                  aria-label="关闭标签"
                  onClick={(e) => {
                    e.stopPropagation()
                    if (id === curFileId && !guardEditing()) return
                    closeTab(id)
                  }}
                >
                  <Icon id="i-x" />
                </button>
              </div>
            )
          })}
          <button className="tab-add" aria-label="打开文档" title="打开文档" onClick={() => guardEditing() && showList()}>
            <Icon id="i-plus" />
          </button>
        </div>
        <div className="tb-actions">
          {editableDoc && !editing && (
            <button
              className="tb-btn"
              aria-label="编辑文档"
              title={editTitle}
              onClick={() => setEditing(true)}
            >
              <Icon id="i-edit" />
            </button>
          )}
          {copyEditableDoc && !editing && (
            <button
              className="tb-btn"
              aria-label="创建可编辑副本"
              title="创建可编辑副本后编辑（原件保持不变）"
              disabled={copyBusy}
              onClick={() => void beginCopyEdit()}
            >
              <Icon id="i-edit" />
            </button>
          )}
          {doc?.isArtifact && (
            <div style={{ position: 'relative' }}>
              <button
                className="tb-btn"
                aria-label="导出产物"
                title="导出产物（按产物类型提供原格式与可转换格式）"
                aria-expanded={exportOpen}
                onClick={() => setExportOpen(!exportOpen)}
              >
                <Icon id="i-arrow" />
              </button>
              {exportOpen && (
                <div className="art-pop" role="menu" aria-label="导出格式" style={{ right: 0, top: '34px' }}>
                  <div className="art-list">
                    {exportOptionsFor({ kind: artMeta?.kind ?? 'report', title: doc.name }).map(
                      ({ fmt, label, hint }) => (
                        <div
                          key={fmt}
                          className="art-item"
                          role="menuitem"
                          tabIndex={0}
                          title={hint}
                          onClick={() => doExport(fmt as ArtifactExportFormat)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') doExport(fmt as ArtifactExportFormat)
                          }}
                        >
                          {label}
                        </div>
                      )
                    )}
                    {artifactIsViz &&
                      graphicOptions().map(({ fmt, label, hint }) => (
                        <div
                          key={fmt}
                          className="art-item"
                          role="menuitem"
                          tabIndex={0}
                          title={hint}
                          onClick={() => void doGraphicExport(fmt as 'svg' | 'png')}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') void doGraphicExport(fmt as 'svg' | 'png')
                          }}
                        >
                          {label}
                        </div>
                      ))}
                  </div>
                </div>
              )}
            </div>
          )}
          <button
            className="icon-btn"
            aria-label={fullscreen ? '退出全屏' : '全屏阅读'}
            title={fullscreen ? '退出全屏' : '全屏阅读'}
            onClick={() => setFullscreen(!fullscreen)}
          >
            <Icon id={fullscreen ? 'i-shrink' : 'i-expand'} />
          </button>
          <button className="icon-btn" aria-label="隐藏面板" title="隐藏面板" onClick={toggleRight}>
            <Icon id="i-panel" />
          </button>
        </div>
      </div>

      <div className="pv-filecap">
        <span className="pv-filecap-name">{doc?.name ?? '—'}</span>
        {docgenArtifact && doc?.docgenDraftId && (
          // 工作台操作迁入预览工具栏（升级方案 §10 阶段二）：从草稿导出件一键回到
          // 文书工作台，继续生成正文/定稿/版本比较与恢复，无需退回文档列表
          <button
            className="pv-edit-btn"
            title="回到文书工作台（正文生成、定稿、版本比较与恢复）"
            onClick={() => openDocGen(doc.docgenDraftId!)}
          >
            <Icon id="i-doc" />
            文书工作台
          </button>
        )}
        {editableDoc && !editing && (
          <button
            className="pv-edit-btn"
            title={editTitle}
            onClick={() => setEditing(true)}
          >
            <Icon id="i-edit" />
            编辑
          </button>
        )}
        {copyEditableDoc && !editing && (
          <button
            className="pv-edit-btn"
            title="创建可编辑副本后编辑（原件保持不变）"
            disabled={copyBusy}
            onClick={() => void beginCopyEdit()}
          >
            <Icon id="i-edit" />
            {copyBusy ? '创建副本…' : '复制并编辑'}
          </button>
        )}
        {editing && <span className="pv-editing">{docgenArtifact ? '编辑中 · 仅修改导出文件' : '编辑中'}</span>}
      </div>

      {isRedacted && (
        <div className="redact-banner" role="status">
          <Icon id="i-eye-off" />
          <span>
            已脱敏 · 本篇 <b>{maskedCount}</b> 处敏感信息已星号打码，「文本」视图黄底显示；「原件」预览为原始文件
          </span>
        </div>
      )}

      <div className="pv-body">
        {editing && doc ? (
          <ArtifactEditor
            artifactId={editArtifactId ?? doc.id}
            onClose={() => {
              setEditing(false)
              setEditArtifactId(null)
            }}
          />
        ) : (
          <>
        <div className="pv-scroll" ref={scrollRef}>
          <div
            className={`paper${viewMode === 'render' && (isRealDoc || isFileArtifact || isHtmlDoc) ? ' paper-render' : ''}`}
            ref={paperRef}
            key={curFileId ?? 'none'}
          >
            {isFileArtifact ? (
              canRenderOriginal ? (
                <OriginalPreview
                  docId={doc.id}
                  kind={doc.kind}
                  pageCount={1}
                  zoom={zoom}
                  renderFrame={renderFrame}
                  source="artifact"
                  title={doc.name}
                />
              ) : (
                <div className="doc-render">
                  <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>
                    该文件格式（{doc.kind.toUpperCase()}）暂不支持在线预览。
                  </p>
                  {doc.body?.fileName && (
                    <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
                      文件已保存到本地工作空间：{doc.body.fileName}，可在工作空间目录中打开。
                    </p>
                  )}
                </div>
              )
            ) : doc?.isArtifact ? (
              doc.body ? (
                <BodyView body={doc.body} onCite={openCitation} onPending={openClaimsQueue} />
              ) : (
                <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
                  正在加载产物…
                </p>
              )
            ) : loadingPages ? (
              <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
                正在加载文档…
              </p>
            ) : doc ? (
              viewMode === 'render' ? (
                <OriginalPreview
                  docId={doc.id}
                  kind={doc.kind}
                  pageCount={doc.pages}
                  zoom={zoom}
                  renderFrame={renderFrame}
                  title={doc.name}
                />
              ) : (
                <TextDocument
                  docId={doc.id}
                  pages={docPages[doc.id] ?? []}
                  meta={docPageMeta[doc.id] ?? []}
                  highlightMask={isRedacted}
                />
              )
            ) : (
              <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
                未选择文档
              </p>
            )}
          </div>
        </div>
        <div className="float-toolbar" role="toolbar" aria-label="阅读工具栏">
          {isRealDoc && !isHtmlDoc && (
            <>
              <div className="tb-modes" role="group" aria-label="阅读呈现方式">
                <button
                  className={`tb-mode${viewMode === 'render' ? ' on' : ''}`}
                  aria-pressed={viewMode === 'render'}
                  disabled={!canRenderOriginal}
                  title={
                    canRenderOriginal
                      ? '按原件真实格式预览（PDF / Word / Excel / Markdown / HTML / 图片）'
                      : '该格式暂不支持原件预览，仅可查看提取文本'
                  }
                  onClick={() => setPreviewMode('render')}
                >
                  原件
                </button>
                <button
                  className={`tb-mode${viewMode === 'text' ? ' on' : ''}`}
                  aria-pressed={viewMode === 'text'}
                  title="Core 提取的页文本（含 OCR 识别结果与人工更正）"
                  onClick={() => setPreviewMode('text')}
                >
                  文本
                </button>
              </div>
              <span className="tb-sep"></span>
            </>
          )}
          <button className="tb-btn" aria-label="缩小" onClick={() => setZoom(zoom - 0.1)}>
            <Icon id="i-minus" />
          </button>
          <span className="ft-zoom">{Math.round(zoom * 100)}%</span>
          <button className="tb-btn" aria-label="放大" onClick={() => setZoom(zoom + 0.1)}>
            <Icon id="i-plus" />
          </button>
          <span className="tb-sep"></span>
          <input
            className="ft-page"
            type="number"
            min={1}
            value={pageInput}
            aria-label="跳转页码"
            onChange={(e) => setPageInput(e.target.value)}
            onBlur={(e) => jumpPage(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && jumpPage((e.target as HTMLInputElement).value)}
          />
          <span className="ft-total">
            / <b>{doc?.pages || 1}</b>
          </span>
          <span className="tb-sep"></span>
          <div className={`ft-searchwrap${docSearchOpen ? ' open' : ''}`}>
            <button
              className="tb-btn"
              aria-label="文档内搜索"
              title="文档内搜索"
              onClick={() => setDocSearch(!docSearchOpen, docSearchOpen ? '' : docSearchTerm)}
            >
              <Icon id="i-search" />
            </button>
            <input
              type="text"
              placeholder="文档内搜索"
              aria-label="文档内搜索"
              value={docSearchTerm}
              onChange={(e) => setDocSearch(true, e.target.value)}
            />
          </div>
        </div>
          </>
        )}
      </div>

      {previewDoc && preview && (
        <div className="tab-preview" style={{ left: preview.x, top: preview.y }}>
          <h4>
            <span className={`ftype ${previewDoc.type}`}>{badge(previewDoc.type)}</span>
            {previewDoc.name}
          </h4>
          <div className="tp-meta">
            {previewDoc.type.toUpperCase()} · {previewDoc.pages} 页 · {previewDoc.date}
          </div>
          <div className="tp-snip">{docSnippet(previewDoc)}</div>
        </div>
      )}
    </div>
  )
}

function docSnippet(doc: { body?: { intro?: string; report?: { overview?: string } }; pageTexts?: string[] }): string {
  const artifactText = doc.body?.report?.overview ?? doc.body?.intro ?? ''
  const raw = artifactText || (doc.pageTexts?.[0] ?? '')
  return raw.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').slice(0, 64) + '…'
}
