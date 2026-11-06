import { useEffect, useRef, useState, type JSX } from 'react'

/**
 * Word（DOCX）原件预览（REQ-DOC-014）：docx-preview 按 OOXML 还原真实版式。
 *
 * 逐页分页渲染（breakPages）：页尺寸、页边距、分页符、页眉页脚与编号按原文档
 * 版式呈现，接近 Word 原生观感；页宽固定为文档版心，整体按面板宽度自适应缩放
 * （fit），再叠加外层用户 zoom。
 * 渲染为真实 DOM 文本，文档内搜索与选区批注锚定均可用。
 */
export function DocxPreview({ blob, zoom }: { blob: Blob; zoom: number }): JSX.Element {
  const wrapRef = useRef<HTMLDivElement>(null)
  const hostRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [fit, setFit] = useState(1)

  // 测量用的当前缩放状态（fit × 用户 zoom）：rect 为缩放后视觉宽度，需除回本地页宽
  const appliedRef = useRef(zoom)
  appliedRef.current = fit * zoom

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let cancelled = false
    setReady(false)
    setError(null)
    setFit(1)

    void (async () => {
      try {
        const { renderAsync } = await import('docx-preview')
        if (cancelled) return
        await renderAsync(blob, host, host, {
          inWrapper: true,
          className: 'docx',
          ignoreWidth: false,
          ignoreHeight: false,
          breakPages: true,
          ignoreLastRenderedPageBreak: false,
          renderHeaders: true,
          renderFooters: true,
          renderFootnotes: true,
          renderEndnotes: true,
          useBase64URL: false
        })
        if (cancelled) return
        setReady(true)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()

    return () => {
      cancelled = true
      host.replaceChildren()
    }
  }, [blob])

  // 自适应：页宽是文档版心（不随容器变），按面板宽度算 fit；面板尺寸变化时重算
  useEffect(() => {
    const wrap = wrapRef.current
    const host = hostRef.current
    if (!wrap || !host || !ready) return
    const measure = (): void => {
      const page = host.querySelector<HTMLElement>('section.docx')
      if (page) {
        const nativeW = page.getBoundingClientRect().width / (appliedRef.current || 1)
        if (nativeW > 0) {
          const next = Math.min(2, Math.max(0.2, wrap.clientWidth / nativeW))
          setFit(next)
          appliedRef.current = next * zoom
        }
      }
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(wrap)
    return () => ro.disconnect()
  }, [ready, zoom])

  return (
    <div className="doc-render docx-render" ref={wrapRef}>
      <div className="docx-zoom" style={{ zoom: String(fit * zoom) }}>
        <div className="docx-host" ref={hostRef} />
      </div>
      {!ready && !error && (
        <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
          正在渲染 Word 原件…
        </p>
      )}
      {error && (
        <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>
          Word 原件预览失败：{error}
        </p>
      )}
    </div>
  )
}
