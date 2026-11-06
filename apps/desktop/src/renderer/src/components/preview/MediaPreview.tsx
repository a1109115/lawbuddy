import { useEffect, useRef, useState, type JSX, type PointerEvent as RPointerEvent, type WheelEvent as RWheelEvent } from 'react'
import { getBridge } from '../../services/bridge'
import { Icon } from '../Icon'

/**
 * 图片查看器：缩放（滚轮/按钮）、拖动、旋转 90°、适合窗口。
 *
 * 只接收 blob URL（Renderer 不接触文件路径）；SVG 以 <img> 方式加载，
 * 不直接注入可执行 DOM，避免脚本执行面扩大。
 */
export function ImageViewer({
  url,
  label,
  zoom
}: {
  url: string
  label: string
  /** 外层阅读缩放（与图片自身缩放相乘） */
  zoom: number
}): JSX.Element {
  const [scale, setScale] = useState(1)
  const [rot, setRot] = useState(0)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [fit, setFit] = useState(true)
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)

  const reset = (): void => {
    setScale(1)
    setRot(0)
    setOffset({ x: 0, y: 0 })
    setFit(true)
  }

  // 滚轮缩放：以容器中心为基准，阻止页面滚动
  const onWheel = (e: RWheelEvent<HTMLDivElement>): void => {
    if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < 2) return
    e.preventDefault()
    setFit(false)
    setScale((s) => Math.min(5, Math.max(0.2, s * (e.deltaY < 0 ? 1.1 : 0.9))))
  }

  const onPointerDown = (e: RPointerEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return
    drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: RPointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (!d) return
    setFit(false)
    setOffset({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) })
  }
  const onPointerUp = (e: RPointerEvent<HTMLDivElement>): void => {
    drag.current = null
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
  }

  const effective = zoom * scale
  return (
    <div className="doc-render img-viewer">
      <div className="img-toolbar" role="toolbar" aria-label="图片查看工具栏">
        <button className="img-tb" title="缩小" onClick={() => { setFit(false); setScale((s) => Math.max(0.2, s * 0.9)) }}>
          <Icon id="i-minus" />
        </button>
        <span className="img-pct">{Math.round(effective * 100)}%</span>
        <button className="img-tb" title="放大" onClick={() => { setFit(false); setScale((s) => Math.min(5, s * 1.1)) }}>
          <Icon id="i-plus" />
        </button>
        <button className="img-tb" title="旋转 90°" onClick={() => setRot((r) => (r + 90) % 360)}>
          <Icon id="i-rotate" />
        </button>
        <button className={`img-tb${fit ? ' on' : ''}`} title="适合窗口" onClick={reset}>
          适应
        </button>
      </div>
      <div
        className="img-stage"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      >
        <img
          className={`doc-image${drag.current ? ' dragging' : ''}`}
          src={url}
          alt={label}
          draggable={false}
          style={{
            width: fit ? '92%' : `${Math.round(effective * 100)}%`,
            transform: `translate(${offset.x}px, ${offset.y}px) rotate(${rot}deg)`
          }}
        />
      </div>
      <p className="img-hint">滚轮或按钮缩放 · 拖动平移 · 可旋转</p>
    </div>
  )
}

/** 音频/视频：通过 lawbuddy-media:// 协议按 Range 流式读取（不整份载入内存） */
export function MediaPreview({
  docId,
  kind,
  source,
  title
}: {
  docId: string
  kind: string
  source: 'document' | 'artifact'
  title?: string
}): JSX.Element {
  const [url, setUrl] = useState('')
  const [size, setSize] = useState(0)
  const [error, setError] = useState('')
  const [failed, setFailed] = useState('')
  const contentIdRef = useRef<string | null>(null)
  const isAudio = kind === 'audio'

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge) {
      setError('本地服务未连接，无法播放媒体')
      return
    }
    let alive = true
    setError('')
    setFailed('')
    setUrl('')
    void (async () => {
      try {
        const info =
          source === 'artifact'
            ? await bridge.artifact.openContent({ artifactId: docId })
            : await bridge.document.openContent({ documentId: docId })
        if (!alive) {
          void bridge.document.closeContent(info.contentId).catch(() => undefined)
          return
        }
        contentIdRef.current = info.contentId
        setSize(info.sizeBytes ?? 0)
        setUrl(`lawbuddy-media://${info.contentId}`)
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      alive = false
      const cid = contentIdRef.current
      contentIdRef.current = null
      if (cid) void bridge.document.closeContent(cid).catch(() => undefined)
    }
  }, [docId, source])

  if (error) {
    return <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>媒体预览失败：{error}</p>
  }

  const mb = size > 0 ? `${(size / 1024 / 1024).toFixed(1)} MB` : ''
  if (!url) {
    return <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>正在准备{isAudio ? '音频' : '视频'}…</p>
  }
  return (
    <div className={`doc-render media-render${isAudio ? ' audio' : ''}`}>
      {isAudio ? (
        <div className="media-audio-card">
          <Icon id="i-music" />
          <div className="media-audio-title">{title || '音频'}</div>
          <audio
            className="media-audio"
            src={url}
            controls
            preload="metadata"
            onError={() => setFailed('当前音频编码无法在本地解码播放，请用系统播放器打开原文件。')}
          />
          {mb && <div className="media-audio-meta">{mb}</div>}
        </div>
      ) : (
        <video
          className="media-video"
          src={url}
          controls
          preload="metadata"
          onError={() => setFailed('当前视频容器或编码无法在本地解码播放，请用系统播放器打开原文件。')}
        />
      )}
      {failed && (
        <p className="doc-p doc-indent" role="alert" style={{ color: 'var(--red-500)' }}>
          {failed}
        </p>
      )}
      <p className="img-hint">支持拖动进度条按需读取，不会把整个文件载入内存</p>
    </div>
  )
}
