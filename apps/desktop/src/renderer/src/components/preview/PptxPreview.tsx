import { useEffect, useRef, useState, type JSX } from 'react'

/** 预览器实例的最小结构（pptx-preview 未从包根导出实例类型） */
interface PptxViewer {
  destroy(): void
}

/**
 * PPT（PPTX）原件预览：pptx-preview 薄适配（AI 生成 / 用户导入的幻灯片按原版式预览）。
 *
 * - 库按幻灯片逐页渲染（mode=list，纵向滚动全部页）；缩放由外层 zoom 控制；
 * - 动态 import（图表渲染依赖 echarts，体积较大，只在真正预览 PPT 时加载）；
 * - 组件卸载或 blob 变化时 destroy 预览器实例并清空宿主容器。
 */
export function PptxPreview({ blob, zoom }: { blob: Blob; zoom: number }): JSX.Element {
  const hostRef = useRef<HTMLDivElement>(null)
  const viewerRef = useRef<PptxViewer | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let cancelled = false
    setReady(false)
    setError(null)

    void (async () => {
      try {
        const buffer = await blob.arrayBuffer()
        const { init } = await import('pptx-preview')
        if (cancelled) return
        const viewer = init(host, { mode: 'list' })
        viewerRef.current = viewer
        await viewer.preview(buffer)
        if (cancelled) {
          viewer.destroy()
          return
        }
        setReady(true)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      }
    })()

    return () => {
      cancelled = true
      viewerRef.current?.destroy()
      viewerRef.current = null
      host.replaceChildren()
    }
  }, [blob])

  return (
    <div className="doc-render docx-render" style={{ zoom: String(zoom) }}>
      <div className="docx-host" ref={hostRef} />
      {!ready && !error && (
        <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
          正在渲染 PPT 原件…
        </p>
      )}
      {error && (
        <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>
          PPT 原件预览失败：{error}
        </p>
      )}
    </div>
  )
}
