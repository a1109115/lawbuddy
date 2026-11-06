import { useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import type { AnnotationColor, DocumentAnnotation } from '@lawbuddy/contracts'
import { useApp, type OcrPageMeta } from '../../state/store'

/** 批注荧光色（红/黄/蓝）选项 */
const ANN_COLORS: Array<{ id: AnnotationColor; label: string }> = [
  { id: 'red', label: '红色标注' },
  { id: 'yellow', label: '黄色标注' },
  { id: 'blue', label: '蓝色标注' }
]

/** 批注内容长度上限（字） */
const ANN_BODY_MAX = 150

/** 批注高亮排除区：锚定只发生在正文容器内，仍排除浮层与既有 mark（搜索高亮/脱敏标记不参与锚定） */
const ANN_EXCLUDE_SEL = '.ann-peek,.ann-pop,mark'

/** 预览气泡文本截断 */
function truncText(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s
}

/** 空白折叠规范化（与选中 quote 的 `\s+→' '` 语义一致）：
 * 连续空白折叠为单空格，并返回「规范化下标 → 原文下标」映射（折叠空白映射到最后一个空白字符） */
function foldWhitespace(orig: string): { norm: string; map: number[] } {
  const chars: string[] = []
  const map: number[] = []
  let i = 0
  while (i < orig.length) {
    if (/\s/.test(orig[i])) {
      while (i + 1 < orig.length && /\s/.test(orig[i + 1])) i++
      chars.push(' ')
      map.push(i)
      i++
    } else {
      chars.push(orig[i])
      map.push(i)
      i++
    }
  }
  return { norm: chars.join(''), map }
}

/** 收集页内可锚定文本节点，构建规范化正文 haystack 与节点回映表；
 * 节点间以单空格衔接（块级边界换行折叠语义） */
function buildAnnHaystack(el: HTMLElement): { hay: string; slots: Array<{ node: Text; start: number; norm: string; map: number[] }> } {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const t = n as Text
      const parent = t.parentElement
      if (!parent || !t.nodeValue || !t.nodeValue.trim()) return NodeFilter.FILTER_REJECT
      if (parent.closest(ANN_EXCLUDE_SEL)) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    }
  })
  let hay = ''
  const slots: Array<{ node: Text; start: number; norm: string; map: number[] }> = []
  let cur: Node | null
  while ((cur = walker.nextNode())) {
    const node = cur as Text
    const { norm, map } = foldWhitespace(node.nodeValue ?? '')
    if (!norm.trim()) continue
    slots.push({ node, start: hay.length, norm, map })
    hay += `${norm} `
  }
  return { hay, slots }
}

/** 在规范化正文中定位 quote 的全部不重叠出现（已被其他批注占用的区间跳过） */
function findAnnRanges(hay: string, quote: string, taken: Array<[number, number]>): Array<[number, number]> {
  const q = quote.replace(/\s+/g, ' ').trim()
  if (!q) return []
  const out: Array<[number, number]> = []
  let from = 0
  for (;;) {
    const i = hay.indexOf(q, from)
    if (i < 0) break
    from = i + 1
    if (taken.some(([s, e]) => i < e && i + q.length > s)) continue
    out.push([i, i + q.length])
  }
  return out
}

/** 把批注 quote 在页内正文涂上荧光 mark：按 start 降序应用（splitText 偏移安全），重叠跳过 */
function applyAnnMarks(el: HTMLElement, anns: DocumentAnnotation[]): void {
  const { hay, slots } = buildAnnHaystack(el)
  if (!slots.length) return
  const taken: Array<[number, number]> = []
  const jobs: Array<{ start: number; end: number; ann: DocumentAnnotation }> = []
  for (const a of anns) {
    for (const [s, e] of findAnnRanges(hay, a.quote ?? '', taken)) {
      jobs.push({ start: s, end: e, ann: a })
      taken.push([s, e])
    }
  }
  jobs.sort((x, y) => y.start - x.start)
  for (const job of jobs) {
    const color = job.ann.color ?? 'yellow'
    for (const slot of slots) {
      const sEnd = slot.start + slot.norm.length
      if (job.start >= sEnd || job.end <= slot.start) continue
      const ns = Math.max(job.start, slot.start) - slot.start
      const ne = Math.min(job.end, sEnd) - slot.start
      const oStart = slot.map[ns]
      const oEnd = slot.map[ne - 1] + 1
      if (oEnd <= oStart) continue
      const mark = document.createElement('mark')
      mark.className = `ann-mark c-${color}`
      mark.dataset.ann = job.ann.id
      const tail = slot.node.splitText(oStart)
      tail.splitText(oEnd - oStart)
      tail.parentNode?.insertBefore(mark, tail)
      mark.appendChild(tail)
    }
  }
}

/** 清除页内批注荧光（只拆 mark[data-ann]，保留文本内容） */
function clearAnnMarks(el: HTMLElement): void {
  el.querySelectorAll('mark[data-ann]').forEach((m) => {
    m.replaceWith(document.createTextNode(m.textContent ?? ''))
  })
  el.normalize()
}

/** 荧光色选择器（红/黄/蓝三色圆点单选） */
function ColorSwatches({ value, onChange }: { value: AnnotationColor; onChange: (c: AnnotationColor) => void }): JSX.Element {
  return (
    <div className="ann-colors" role="radiogroup" aria-label="标注荧光色">
      <span className="ann-colors-label">荧光色</span>
      {ANN_COLORS.map((c) => (
        <button
          key={c.id}
          type="button"
          role="radio"
          aria-checked={value === c.id}
          aria-label={c.label}
          title={c.label}
          className={`ann-color-dot c-${c.id}${value === c.id ? ' on' : ''}`}
          onClick={() => onChange(c.id)}
        />
      ))}
    </div>
  )
}

/**
 * 页容器（DEV-DOC-010 / O-008）：
 * 页码标识 + OCR 人工更正入口，包住该页的实际内容。
 * 批注交互：悬停荧光预览、点击荧光编辑浮窗、选中文本经浮动工具栏新建。
 *
 * 内容由调用方决定：文本视图给段落，原件预览给 PDF 画布 / 工作表 / Word 版式 /
 * 图片等；批注与更正作用于 Core 的页文本，与呈现方式无关。
 */
export function PageFrame({
  docId,
  pageNo,
  totalPages,
  meta,
  text,
  footnote,
  allowQuoteSelection = true,
  readOnly = false,
  children
}: {
  docId: string
  pageNo: number
  totalPages: number
  /** 该页文本（OCR 更正编辑与批注引用） */
  text: string
  meta?: OcrPageMeta
  /** 页副标识（如工作表名） */
  footnote?: string
  /** 关联案件共享材料（只读）：保留问AI/引用，隐藏 OCR 更正与批注写入 */
  readOnly?: boolean
  /** 原件预览中无法选中页内文本时（沙箱 iframe）传 false：只能手工粘贴锚定原文 */
  allowQuoteSelection?: boolean
  children: ReactNode
}): JSX.Element {
  const annotations = useApp((s) => s.docAnnotations[docId])
  const correctOcr = useApp((s) => s.correctOcr)
  const addAnnotation = useApp((s) => s.addAnnotation)
  const updateAnnotation = useApp((s) => s.updateAnnotation)
  const setAnnotationResolved = useApp((s) => s.setAnnotationResolved)
  const deleteAnnotation = useApp((s) => s.deleteAnnotation)
  const setInput = useApp((s) => s.setInput)
  const focusInput = useApp((s) => s.focusInput)

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  // —— 选中弹出的浮动工具栏（问AI / 批注）：记录视口坐标与选中文本 ——
  const [selTool, setSelTool] = useState<{ left: number; top: number; text: string } | null>(null)
  // —— 荧光交互（参考既定交互稿）：悬停 mark 预览 / 点击 mark 编辑浮窗 / 选中后新建浮窗 ——
  const [peek, setPeek] = useState<{ id: string; left: number; top: number; above: boolean } | null>(null)
  const [editPop, setEditPop] = useState<{ id: string; left: number; top: number } | null>(null)
  const [newPop, setNewPop] = useState<{ left: number; top: number; quote: string } | null>(null)
  const [popBody, setPopBody] = useState('')
  const [popColor, setPopColor] = useState<AnnotationColor>('yellow')
  const [popDelConfirm, setPopDelConfirm] = useState(false)
  const pageRef = useRef<HTMLDivElement>(null)
  /** 正文容器：批注荧光只锚定并观察正文；悬停预览/浮窗等浮层渲染在其外，
   * 开关浮层不会触发 MutationObserver 重涂（重涂会销毁重建光标下的 mark，
   * 导致 mouseout 丢失（气泡残留）与 click 目标漂移（点击不弹编辑窗）） */
  const bodyRef = useRef<HTMLDivElement>(null)

  const pageAnns = useMemo(() => (annotations ?? []).filter((a) => a.pageNo === pageNo), [annotations, pageNo])
  const peekAnn = peek ? pageAnns.find((a) => a.id === peek.id) : undefined
  const editAnn = editPop ? pageAnns.find((a) => a.id === editPop.id) : undefined
  // 仅 OCR 页可更正：原生文本层与空页不提供入口（DEV-DOC-010 / O-008）
  const isOcr = !!meta && (meta.quality === 'ocr' || meta.quality === 'ocr_pending')

  /** 选区 → 批注锚定原文 / 问AI 文本：仅接受完全落在本页内的选区（Core 按 Q-010 规范化重定位） */
  const selectionInPage = (): string => {
    if (!allowQuoteSelection) return ''
    const sel = window.getSelection()
    const el = pageRef.current
    if (!sel || sel.isCollapsed || !el) return ''
    if (!el.contains(sel.anchorNode) || !el.contains(sel.focusNode)) return ''
    return sel.toString().replace(/\s+/g, ' ').trim().slice(0, 500)
  }

  /** 选中后弹出浮动工具栏：定位到选区上方（顶部空间不足时放到下方） */
  const handleMouseUp = (): void => {
    const picked = selectionInPage()
    if (!picked) {
      setSelTool(null)
      return
    }
    const sel = window.getSelection()
    const rect = sel && sel.rangeCount > 0 ? sel.getRangeAt(0).getBoundingClientRect() : null
    if (!rect || (rect.width === 0 && rect.height === 0)) {
      setSelTool(null)
      return
    }
    const w = 148
    const left = Math.min(Math.max(8, rect.left + rect.width / 2 - w / 2), window.innerWidth - w - 8)
    const top = rect.top > 72 ? rect.top - 40 : rect.bottom + 8
    setSelTool({ left, top, text: picked })
  }

  // 选区消失（点击他处 / 开始新选择）或滚动时收起浮动工具栏
  useEffect(() => {
    if (!selTool) return
    const onSelChange = (): void => {
      const sel = window.getSelection()
      if (!sel || sel.isCollapsed) setSelTool(null)
    }
    const onScroll = (): void => setSelTool(null)
    document.addEventListener('selectionchange', onSelChange)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('selectionchange', onSelChange)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [selTool])

  // —— 正文荧光高亮（DEV-DOC-010）：按批注 quote 在正文容器内定位并涂色；
  // 异步加载的内容（原件画布/产物正文）由 MutationObserver 触发重涂；
  // 只观察正文容器，浮层（预览气泡/浮窗/工具栏）的开关不会引发重涂 ——
  useEffect(() => {
    const el = bodyRef.current
    if (!el) return
    let raf = 0
    const obs = new MutationObserver(() => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(paint)
    })
    /** 重涂前先断开观察：paint 自身的 DOM 变更不再回流触发下一轮。
     * 否则「拆掉重建 mark → 变更入队 → 回调（异步送达时 paused 已复位）→ 再拆再建」
     * 会以每帧节奏无限重涂，光标下的 mark 持续销毁重建：
     * mousedown/mouseup 落在不同 mark 实例上，click 派发到公共祖先，点击荧光永远弹不出编辑窗 */
    const paint = (): void => {
      obs.disconnect()
      try {
        clearAnnMarks(el)
        applyAnnMarks(el, pageAnns)
      } finally {
        obs.observe(el, { childList: true, subtree: true, characterData: true })
      }
    }
    paint()
    return () => {
      cancelAnimationFrame(raf)
      obs.disconnect()
      clearAnnMarks(el)
    }
  }, [pageAnns])

  /** 问AI：选中内容装配为问题填入输入坞并聚焦，交给 LawBuddy 分析 */
  const askAi = (): void => {
    const text = selTool?.text.trim()
    setSelTool(null)
    if (!text) return
    setInput(`请结合案件材料，分析文档中这段内容：\n「${text}」`)
    focusInput()
    window.getSelection()?.removeAllRanges()
  }

  /** 批注（选中 → 浮动工具栏入口）：在工具栏位置弹出新建批注浮窗，锚定原文自动带入 */
  const startAnnotate = (): void => {
    const st = selTool
    setSelTool(null)
    if (!st) return
    setPeek(null)
    setEditPop(null)
    setPopDelConfirm(false)
    setPopBody('')
    setPopColor('yellow')
    setNewPop({
      quote: st.text,
      left: Math.min(Math.max(8, st.left + 74 - 180), window.innerWidth - 376),
      top: Math.min(Math.max(8, st.top + 44), window.innerHeight - 300)
    })
  }

  // —— 荧光 mark 交互（事件委托：重涂会重建 mark，监听须挂在页容器上）——
  const markFromEvent = (e: React.MouseEvent): HTMLElement | null =>
    (e.target as HTMLElement | null)?.closest?.('mark[data-ann]') ?? null

  /** 悬停 mark：弹出预览气泡（上方空间足够放上方，否则下方）；移出荧光范围即收起 */
  const handleMarkOver = (e: React.MouseEvent): void => {
    if (editPop || newPop) return
    const m = markFromEvent(e)
    if (!m) {
      if (peek) setPeek(null)
      return
    }
    const ann = pageAnns.find((a) => a.id === m.dataset.ann)
    if (!ann) return
    const r = m.getBoundingClientRect()
    const above = r.top > 170
    setPeek({
      id: ann.id,
      left: Math.min(Math.max(210, r.left + r.width / 2), window.innerWidth - 210),
      top: above ? r.top - 8 : r.bottom + 8,
      above
    })
  }

  const handleMarkOut = (e: React.MouseEvent): void => {
    const m = markFromEvent(e)
    if (!m) return
    const to = e.relatedTarget as Node | null
    if (to && m.contains(to)) return
    setPeek(null)
  }

  /** 点击 mark：收起选区并打开该批注的编辑浮窗 */
  const handleMarkClick = (e: React.MouseEvent): void => {
    if (readOnly) return
    const m = markFromEvent(e)
    if (!m) return
    const ann = pageAnns.find((a) => a.id === m.dataset.ann)
    if (!ann) return
    e.stopPropagation()
    window.getSelection()?.removeAllRanges()
    setSelTool(null)
    setPeek(null)
    setNewPop(null)
    setPopDelConfirm(false)
    setPopBody(ann.body)
    setPopColor(ann.color ?? 'yellow')
    setEditPop({
      id: ann.id,
      left: Math.min(Math.max(8, m.getBoundingClientRect().left), window.innerWidth - 376),
      top: Math.min(Math.max(8, m.getBoundingClientRect().bottom + 8), window.innerHeight - 300)
    })
  }

  /** 浮窗内 Esc：关闭浮窗并阻断阅读视图层级 Esc；中文组词中的 Esc 不拦截 */
  const escPop = (e: React.KeyboardEvent): void => {
    if (e.key === 'Escape' && !(e.nativeEvent as KeyboardEvent).isComposing) {
      e.stopPropagation()
      setEditPop(null)
      setNewPop(null)
    }
  }

  const saveEditPop = async (): Promise<void> => {
    if (!editPop || !popBody.trim() || busy) return
    setBusy(true)
    const ok = await updateAnnotation(docId, editPop.id, popBody, popColor)
    setBusy(false)
    if (ok) setEditPop(null)
  }

  const confirmDelPop = async (): Promise<void> => {
    if (!editPop) return
    const id = editPop.id
    setEditPop(null)
    await deleteAnnotation(docId, id)
  }

  /** 浮窗内切换处理状态：与列表时代的「标记已处理/重开」同语义 */
  const toggleResolvedPop = async (): Promise<void> => {
    if (!editPop || !editAnn) return
    await setAnnotationResolved(docId, editPop.id, editAnn.status !== 'resolved')
  }

  const saveNewPop = async (): Promise<void> => {
    if (!newPop || !popBody.trim() || busy) return
    setBusy(true)
    const ok = await addAnnotation(docId, pageNo, popBody, newPop.quote || undefined, popColor)
    setBusy(false)
    if (ok) {
      setNewPop(null)
      window.getSelection()?.removeAllRanges()
    }
  }

  // 点击浮窗与荧光 mark 之外关闭浮窗；滚动时收起浮窗与预览
  useEffect(() => {
    if (!editPop && !newPop) return
    const onDown = (e: MouseEvent): void => {
      const t = e.target as HTMLElement | null
      if (t?.closest?.('.ann-pop') || t?.closest?.('mark[data-ann]')) return
      setEditPop(null)
      setNewPop(null)
    }
    const onScroll = (): void => {
      setEditPop(null)
      setNewPop(null)
      setPeek(null)
    }
    document.addEventListener('mousedown', onDown)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [editPop, newPop])

  // 预览气泡兜底收起：在荧光之外按下鼠标或页面滚动时关闭，
  // 防止浏览器边界事件丢失（如 mark 被重建）导致气泡残留
  useEffect(() => {
    if (!peek) return
    const onDown = (e: MouseEvent): void => {
      const t = e.target as HTMLElement | null
      if (t?.closest?.('mark[data-ann]')) return
      setPeek(null)
    }
    const onScroll = (): void => setPeek(null)
    document.addEventListener('mousedown', onDown)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [peek])

  // 编辑中的批注被删除（列表刷新后消失）时自动关闭浮窗
  useEffect(() => {
    if (editPop && !pageAnns.some((a) => a.id === editPop.id)) setEditPop(null)
  }, [editPop, pageAnns])

  const save = async (): Promise<void> => {
    if (!draft.trim() || busy) return
    setBusy(true)
    await correctOcr(docId, pageNo, draft, note.trim() || undefined)
    setBusy(false)
    setEditing(false)
  }

  return (
    <div
      className="doc-page"
      data-page={pageNo}
      ref={pageRef}
      onMouseUp={handleMouseUp}
      onMouseOver={handleMarkOver}
      onMouseOut={handleMarkOut}
      onClick={handleMarkClick}
    >
      {totalPages > 1 && (
        <div className="page-badge" aria-hidden="true">
          — 第 {pageNo} 页 / 共 {totalPages} 页 —
        </div>
      )}
      {footnote && <div className="page-footnote">{footnote}</div>}
      {isOcr && (
        <div className="ocr-fix-bar">
          <span className="ocr-tag" title="该页文本由 OCR 识别生成，可能含识别错误">
            OCR
            {meta?.ocrConfidence != null ? ` · 置信度 ${Math.round(meta.ocrConfidence * 100)}%` : ''}
          </span>
          {meta?.corrected && (
            <span className="st-badge st-neutral" title="已人工更正；原识别结果保留在更正历史中">
              已更正
            </span>
          )}
          {!editing && !readOnly && (
            <button
              className="btn-ghost ocr-fix-btn"
              title="人工更正本页文本（原件与页图不变）"
              onClick={() => {
                setEditing(true)
                setDraft(text)
                setNote('')
              }}
            >
              更正文本
            </button>
          )}
        </div>
      )}
      {/* 选中文字后弹出的浮动工具栏：问AI / 批注 */}
      {selTool && (
        <div
          className="sel-tool"
          role="toolbar"
          aria-label="选中文本操作"
          style={{ left: selTool.left, top: selTool.top }}
        >
          <button className="sel-tool-btn" onMouseDown={(e) => e.preventDefault()} onClick={askAi}>
            问AI
          </button>
          {!readOnly && (
            <>
              <span className="sel-tool-sep" aria-hidden="true" />
              <button className="sel-tool-btn" onMouseDown={(e) => e.preventDefault()} onClick={startAnnotate}>
                批注
              </button>
            </>
          )}
        </div>
      )}
      {/* 悬停荧光 mark 的预览气泡：引用原文 + 批注内容（已处理带前缀） */}
      {peek && peekAnn && !editPop && !newPop && (
        <div
          className={`ann-peek${peek.above ? ' above' : ''}`}
          style={{ left: peek.left, top: peek.top }}
          role="tooltip"
        >
          {peekAnn.quote && <span className="ann-peek-quote">「{truncText(peekAnn.quote, 80)}」</span>}
          <span className="ann-peek-body">
            {peekAnn.status === 'resolved' ? '【已处理】' : ''}
            {truncText(peekAnn.body, 140)}
          </span>
        </div>
      )}
      {/* 点击荧光 mark / 选中后「批注」弹出的批注浮窗（编辑 / 新建） */}
      {editPop && editAnn && (
        <AnnPopCard
          pos={editPop}
          heading="批注"
          quote={editAnn.quote}
          body={popBody}
          color={popColor}
          busy={busy}
          confirmDel={popDelConfirm}
          deletable
          resolved={editAnn.status === 'resolved'}
          onToggleResolved={() => void toggleResolvedPop()}
          onBody={setPopBody}
          onColor={setPopColor}
          onEsc={escPop}
          onSave={() => void saveEditPop()}
          onCancel={() => setEditPop(null)}
          onAskDelete={() => setPopDelConfirm(true)}
          onConfirmDelete={() => void confirmDelPop()}
        />
      )}
      {newPop && (
        <AnnPopCard
          pos={newPop}
          heading="原文批注"
          quote={newPop.quote}
          body={popBody}
          color={popColor}
          busy={busy}
          confirmDel={false}
          deletable={false}
          onBody={setPopBody}
          onColor={setPopColor}
          onEsc={escPop}
          onSave={() => void saveNewPop()}
          onCancel={() => setNewPop(null)}
        />
      )}
      {editing && (
        <div className="ocr-fix-edit">
          <textarea
            className="form-input ocr-fix-text"
            rows={8}
            aria-label={`第 ${pageNo} 页更正文本`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <input
            className="form-input ocr-fix-note"
            placeholder="更正说明（可选；仅记录长度与说明标记，不写入正文）"
            aria-label="更正说明"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className="ocr-fix-actions">
            <span className="ocr-fix-hint">
              原件只读；更正后检索与 Agent 使用新文本，原识别值保留可追溯。引语不再可定位的结论将自动转待核实。
            </span>
            <button className="btn-ghost cq-btn" onClick={() => setEditing(false)}>
              取消
            </button>
            <button className="btn-primary cq-btn" disabled={busy || !draft.trim()} onClick={() => void save()}>
              保存更正
            </button>
          </div>
        </div>
      )}
      {/* 正文容器：荧光锚定与重涂观察范围仅限正文；浮层在其外，开关浮层不会触发重涂 */}
      <div className="doc-page-body" ref={bodyRef}>
        {children}
      </div>
    </div>
  )
}

/** 批注浮窗卡片（新建 / 编辑共用，参考既定交互稿）：标题 + 锚定原文 + 内容 + 荧光色 + 保存/删除 */
function AnnPopCard({
  pos,
  heading,
  quote,
  body,
  color,
  busy,
  confirmDel,
  deletable,
  resolved,
  onToggleResolved,
  onBody,
  onColor,
  onEsc,
  onSave,
  onCancel,
  onAskDelete,
  onConfirmDelete
}: {
  pos: { left: number; top: number }
  heading: string
  quote?: string | null
  body: string
  color: AnnotationColor
  busy: boolean
  confirmDel: boolean
  deletable: boolean
  /** 处理状态与切换入口（仅编辑浮窗提供） */
  resolved?: boolean
  onToggleResolved?: () => void
  onBody: (v: string) => void
  onColor: (c: AnnotationColor) => void
  onEsc: (e: React.KeyboardEvent) => void
  onSave: () => void
  onCancel: () => void
  onAskDelete?: () => void
  onConfirmDelete?: () => void
}): JSX.Element {
  return (
    <div className="ann-pop" style={{ left: pos.left, top: pos.top }} onKeyDown={onEsc}>
      <div className="ann-pop-head">
        <span className={`ann-color-dot c-${color}`} aria-hidden="true" />
        <span className="ann-pop-title">{heading}</span>
        {onToggleResolved && (
          <span className={`st-badge ${resolved ? 'st-favorable' : 'fst-pending'}`}>
            {resolved ? '已处理' : '待处理'}
          </span>
        )}
        <button className="btn-ghost ann-pop-x" aria-label="关闭" title="关闭" onClick={onCancel}>
          ×
        </button>
      </div>
      {quote && <blockquote className="ann-pop-quote">「{quote}」</blockquote>}
      <textarea
        className="form-input ann-pop-input"
        rows={3}
        maxLength={ANN_BODY_MAX}
        aria-label={`${heading}内容（150 字以内）`}
        placeholder="批注内容（必填，150 字以内）"
        autoFocus
        value={body}
        onChange={(e) => onBody(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault()
            onSave()
          }
        }}
      />
      <div className="ann-pop-foot">
        <ColorSwatches value={color} onChange={onColor} />
        {onToggleResolved && (
          <button className="btn-ghost cq-btn" onClick={onToggleResolved}>
            {resolved ? '重开' : '标记已处理'}
          </button>
        )}
        <span className="ann-pop-hint">Ctrl+Enter 保存</span>
        {deletable &&
          (confirmDel ? (
            <button className="btn-ghost cq-btn ann-del" onClick={onConfirmDelete}>
              确认删除
            </button>
          ) : (
            <button className="btn-ghost cq-btn ann-del" onClick={onAskDelete}>
              删除
            </button>
          ))}
        <button className="btn-ghost cq-btn" onClick={onCancel}>
          取消
        </button>
        <button className="btn-primary cq-btn" disabled={busy || !body.trim()} onClick={onSave}>
          保存
        </button>
      </div>
    </div>
  )
}

/** 将一行文本中的星号打码串渲染为高亮标记（部分打码如「王*远」单星也高亮） */
function renderMaskedLine(line: string, highlight: boolean, keyPrefix: string): ReactNode {
  if (!highlight) return line
  const parts = line.split(/(\*+)/g)
  if (parts.length === 1) return line
  return parts.map((p, i) =>
    /^\*+$/.test(p) ? (
      <mark key={`${keyPrefix}-${i}`} className="mask-mark" title="已脱敏的敏感信息">
        {p}
      </mark>
    ) : (
      p
    )
  )
}

/** 页文本的段落化呈现（文本视图）：按标题样式与条款序号分段；脱敏版高亮打码位置 */
export function TextParagraphs({ text, highlightMask = false }: { text: string; highlightMask?: boolean }): JSX.Element {
  return (
    <>
      {text
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean)
        .map((line, j) => {
          if (/^#{1,4}\s+/.test(line)) {
            return (
              <p key={j} className="doc-sec">
                {renderMaskedLine(line.replace(/^#{1,4}\s+/, ''), highlightMask, `h${j}`)}
              </p>
            )
          }
          if (/^(第[一二三四五六七八九十百]+条|[一二三四五六七八九十]+、)/.test(line) && line.length < 40) {
            return (
              <p key={j} className="doc-sec">
                {renderMaskedLine(line, highlightMask, `s${j}`)}
              </p>
            )
          }
          return (
            <p key={j} className="doc-p doc-indent">
              {renderMaskedLine(line, highlightMask, `p${j}`)}
            </p>
          )
        })}
    </>
  )
}

/** 文本视图：按 Core 页文本逐页呈现（原件预览不可用或用户主动切换时的视图） */
export function TextDocument({
  docId,
  pages,
  meta,
  highlightMask = false
}: {
  docId: string
  pages: string[]
  meta: OcrPageMeta[]
  /** 脱敏版：高亮星号打码位置，让用户看清哪些信息被打码 */
  highlightMask?: boolean
}): JSX.Element {
  if (pages.length === 0) {
    return (
      <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
        该文档暂无可提取的文本（图片类待 OCR，表格类解析在后续版本支持）。
      </p>
    )
  }
  return (
    <article>
      {pages.map((text, i) => (
        <PageFrame
          key={i}
          docId={docId}
          pageNo={i + 1}
          totalPages={pages.length}
          text={text}
          meta={meta[i]}
        >
          <TextParagraphs text={text} highlightMask={highlightMask} />
        </PageFrame>
      ))}
    </article>
  )
}