import { memo, useEffect, useRef, useState, type CSSProperties, type JSX, type ReactNode } from 'react'
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist'

/**
 * PDF 原件预览（REQ-DOC-014）：pdf.js 逐页栅格化 + 文本层。
 *
 * - 画布按 `缩放 × devicePixelRatio` 渲染，保持清晰；文本层透明覆盖在画布上，
 *   维持原件的**文本可选中**（页批注锚定）与文档内搜索高亮。
 * - 懒渲染：IntersectionObserver 只渲染视口附近的页，远离视口时释放画布（数百页卷宗不占满内存）。
 * - Renderer 只拿到 Blob（来自 Core 会话），不接触文件路径。
 */

type PdfModule = typeof import('pdfjs-dist')

let pdfjsPromise: Promise<PdfModule> | null = null

/** 惰性加载 pdf.js 并挂载 worker 资源（`?url` 由 Vite 产出；file:// 下失败会退化为主线程 worker） */
async function loadPdfjs(): Promise<PdfModule> {
  if (!pdfjsPromise) {
    pdfjsPromise = (async () => {
      const mod = await import('pdfjs-dist')
      const worker = await import('pdfjs-dist/build/pdf.worker.min.mjs?url')
      mod.GlobalWorkerOptions.workerSrc = worker.default
      return mod
    })()
  }
  return pdfjsPromise
}

export const PdfPreview = memo(function PdfPreview({
  blob,
  zoom,
  renderFrame
}: {
  blob: Blob
  /** 阅读视图缩放（0.6 ~ 1.6） */
  zoom: number
  /** 每页外壳由父级提供（PageFrame：页码 / OCR 更正 / 页批注） */
  renderFrame: (pageNo: number, content: ReactNode) => ReactNode
}): JSX.Element {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [sizes, setSizes] = useState<Array<{ width: number; height: number }>>([])
  const [error, setError] = useState<string | null>(null)
  const [avail, setAvail] = useState(0)
  const hostRef = useRef<HTMLDivElement>(null)

  // —— 载入文档并测量各页原始尺寸（用于按容器宽度适配与占位高度） ——
  useEffect(() => {
    let cancelled = false
    let loaded: PDFDocumentProxy | null = null
    setPdf(null)
    setSizes([])
    setError(null)
    void (async () => {
      try {
        const lib = await loadPdfjs()
        const data = new Uint8Array(await blob.arrayBuffer())
        if (cancelled) return
        const doc = await lib.getDocument({ data }).promise
        loaded = doc
        if (cancelled) return
        const measured: Array<{ width: number; height: number }> = []
        for (let i = 1; i <= doc.numPages; i += 1) {
          const page = await doc.getPage(i)
          const vp = page.getViewport({ scale: 1 })
          measured.push({ width: vp.width, height: vp.height })
        }
        if (cancelled) return
        setSizes(measured)
        setPdf(doc)
      } catch (e) {
        if (cancelled) {
          void loaded?.loadingTask.destroy()
          return
        }
        setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [blob])

  // 组件卸载/切换文档时销毁加载任务（worker 侧资源一并回收）
  useEffect(() => {
    return () => {
      if (pdf) void pdf.loadingTask.destroy()
    }
  }, [pdf])

  // —— 容器宽度：用于把页面适配到面板内（窄面板不产生横向滚动） ——
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const ro = new ResizeObserver(() => setAvail(host.clientWidth))
    ro.observe(host)
    setAvail(host.clientWidth)
    return () => ro.disconnect()
  }, [])

  if (error) {
    return (
      <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>
        PDF 原件预览失败：{error}
      </p>
    )
  }
  if (!pdf) {
    return (
      <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
        正在解析 PDF 原件…
      </p>
    )
  }

  const maxWidth = sizes.reduce((m, s) => Math.max(m, s.width), 0)
  const fit = maxWidth > 0 && avail > 0 ? Math.min(1.6, Math.max(0.3, (avail - 4) / maxWidth)) : 1
  const scale = fit * zoom

  return (
    <div className="pdf-render" ref={hostRef}>
      {sizes.map((base, i) => {
        const pageNo = i + 1
        return (
          <div key={pageNo}>
            {renderFrame(pageNo, <PdfPage pdf={pdf} pageNo={pageNo} scale={scale} base={base} />)}
          </div>
        )
      })}
    </div>
  )
})

/** 单页：占位 → 进入视口后渲染画布与文本层（离开视口释放画布） */
const PdfPage = memo(function PdfPage({
  pdf,
  pageNo,
  scale,
  base
}: {
  pdf: PDFDocumentProxy
  pageNo: number
  scale: number
  /** 原始尺寸（scale=1，PDF 点）：用于在渲染前就撑出占位高度，保证页码跳转与滚动位置准确 */
  base: { width: number; height: number }
}): JSX.Element {
  const cardRef = useRef<HTMLDivElement>(null)
  const textRef = useRef<HTMLDivElement>(null)
  const [page, setPage] = useState<PDFPageProxy | null>(null)
  const [visible, setVisible] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setPage(null)
    void pdf
      .getPage(pageNo)
      .then((p) => {
        if (!cancelled) setPage(p)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [pdf, pageNo])

  useEffect(() => {
    const card = cardRef.current
    if (!card) return
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (card.contains(e.target)) setVisible(e.isIntersecting)
      },
      { root: null, rootMargin: '700px 0px' }
    )
    io.observe(card)
    return () => io.disconnect()
  }, [])

  useEffect(() => {
    const card = cardRef.current
    const textDiv = textRef.current
    if (!page || !visible || !card || !textDiv) return
    let disposed = false
    let task: ReturnType<PDFPageProxy['render']> | null = null
    let layer: { cancel: () => void } | null = null
    let canvas: HTMLCanvasElement | null = null

    void (async () => {
      try {
        const viewport = page.getViewport({ scale })
        const dpr = Math.min(3, Math.max(1, window.devicePixelRatio || 1))
        canvas = document.createElement('canvas')
        canvas.className = 'pdf-canvas'
        canvas.width = Math.max(1, Math.floor(viewport.width * dpr))
        canvas.height = Math.max(1, Math.floor(viewport.height * dpr))
        canvas.style.width = `${Math.floor(viewport.width)}px`
        canvas.style.height = `${Math.floor(viewport.height)}px`
        // 画布为命令式插入：文本层由 React 持有，画布始终插在其前
        card.insertBefore(canvas, textDiv)
        task = page.render({
          canvas,
          viewport,
          transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0]
        })
        await task.promise
        if (disposed) return
        textDiv.replaceChildren()
        const textContent = await page.getTextContent()
        if (disposed) return
        const lib = await loadPdfjs()
        if (disposed) return
        const tl = new lib.TextLayer({ textContentSource: textContent, container: textDiv, viewport })
        layer = tl
        await tl.render()
      } catch (e) {
        // 取消/切页属正常流程，不作为错误提示
        const name = (e as { name?: string } | null)?.name
        if (!disposed && name !== 'RenderingCancelledException' && name !== 'AbortException') {
          setFailed(e instanceof Error ? e.message : String(e))
        }
      }
    })()

    return () => {
      disposed = true
      try {
        task?.cancel()
      } catch {
        /* 任务可能已结束 */
      }
      try {
        layer?.cancel()
      } catch {
        /* 文本层可能已结束 */
      }
      canvas?.remove()
      textDiv.replaceChildren()
    }
  }, [page, visible, scale])

  const style: CSSProperties = {
    position: 'relative',
    width: Math.floor(base.width * scale),
    height: Math.floor(base.height * scale),
    // pdf.js 文本层按此变量换算字号与层尺寸（需等于 CSS 像素缩放比）
    ['--total-scale-factor' as never]: String(scale)
  }

  return (
    <div className="pdf-page" ref={cardRef} style={style}>
      <div className="textLayer" ref={textRef} aria-hidden="true" />
      {failed && <p className="pdf-page-err">第 {pageNo} 页渲染失败：{failed}</p>}
    </div>
  )
})