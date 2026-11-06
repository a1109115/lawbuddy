import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { useDocContent } from '../../hooks/useDocContent'
import { getBridge } from '../../services/bridge'
import { resolvePreview, type PreviewCapability } from '../../services/preview'
import { decodeBlobText } from '../../services/textDecode'
import { MarkdownView } from '../markdown/MarkdownView'
import { DocxPreview } from './DocxPreview'
import { MediaPreview, ImageViewer } from './MediaPreview'
import { PdfPreview } from './PdfPreview'
import { PptxPreview } from './PptxPreview'
import { CodePreview, CsvPreview } from './TableCodePreview'
import { XlsxPreview } from './XlsxPreview'

/**
 * 原件预览分发（REQ-DOC-014 → P0 预览内核）：按真实格式渲染，而不是只显示纯文本。
 *
 * 分发逻辑由 `services/preview.ts` 的能力注册表统一决定（渲染器 / 分页 / 文本可选 /
 * 引证可定位 / 降级说明），本文件只负责把能力映射到具体组件：
 *
 * - PDF：pdf.js 逐页栅格化 + 文本层
 * - Word：docx-preview 还原版式
 * - Excel：SheetJS 表格网格（列宽 / 合并单元格 / 工作表分页）
 * - PPT：pptx-preview 逐页渲染幻灯片
 * - Markdown / HTML / CSV / 代码 / 纯文本：对应渲染器（文本按 UTF-8 → GB18030 解码）
 * - 图片：缩放 / 拖动 / 旋转；TIFF 由 Core 逐帧转 PNG
 * - 音视频：lawbuddy-media:// Range 协议流式播放
 *
 * 取字节的通道：Core 只读会话（contentId + 分块 base64），Renderer 不接触文件路径。
 */

function Notice({ text, tone }: { text: string; tone?: 'error' }): JSX.Element {
  return (
    <p className="doc-p doc-indent" style={{ color: tone === 'error' ? 'var(--red-500)' : 'var(--ink-300)' }}>
      {text}
    </p>
  )
}

/** 降级提示：能力注册表判定无法原生预览时，明确告知原因而不是静默空白 */
function FallbackNotice({ cap }: { cap: PreviewCapability }): JSX.Element {
  return (
    <div className="preview-fallback" role="status">
      <p className="doc-p doc-indent">{cap.fallback ?? `${cap.label} 暂不支持预览。`}</p>
      <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
        可切换到「文本」视图查看提取内容；扫描件可在文本视图触发 OCR 后重新查看。
      </p>
    </div>
  )
}

export function OriginalPreview({
  docId,
  kind,
  pageCount,
  zoom,
  renderFrame,
  source = 'document',
  title
}: {
  docId: string
  /** Core 嗅探出的文档类型（pdf/docx/xlsx/md/html/csv/mp4…） */
  kind: string
  pageCount: number
  zoom: number
  renderFrame: (pageNo: number, content: ReactNode) => ReactNode
  /** 内容来源：真实卷宗（document）或文件类产物（artifact，AI 生成文档） */
  source?: 'document' | 'artifact'
  /** 媒体预览展示用标题 */
  title?: string
}): JSX.Element {
  const type = kind.toLowerCase()
  const cap = resolvePreview(type)

  // TIFF 不能由浏览器解码：由 Core 按帧转 PNG，逐帧单独开会话（传 pageNo）
  if (type === 'tiff') {
    return <TiffPreview docId={docId} pageCount={pageCount} zoom={zoom} renderFrame={renderFrame} />
  }

  // 音视频：走 Range 协议，不整份读取，也不占用内容会话的字节循环
  if (cap.renderer === 'audio' || cap.renderer === 'video') {
    return (
      <SinglePage renderFrame={renderFrame}>
        <MediaPreview docId={docId} kind={cap.renderer} source={source} title={title} />
      </SinglePage>
    )
  }

  if (cap.renderer === 'unsupported') return <FallbackNotice cap={cap} />

  return (
    <ContentPreview
      docId={docId}
      type={type}
      cap={cap}
      zoom={zoom}
      renderFrame={renderFrame}
      source={source}
    />
  )
}

/** 需要整份字节的内容预览（PDF/Office/文本/图片） */
function ContentPreview({
  docId,
  type,
  cap,
  zoom,
  renderFrame,
  source
}: {
  docId: string
  type: string
  cap: PreviewCapability
  zoom: number
  renderFrame: (pageNo: number, content: ReactNode) => ReactNode
  source: 'document' | 'artifact'
}): JSX.Element {
  const content = useDocContent(docId, true, undefined, source)

  if (content.status === 'error') {
    return <Notice tone="error" text={`原件预览失败：${content.error ?? '未知错误'}`} />
  }
  if (content.status !== 'ready' || !content.blob || !content.url) {
    return <Notice text="正在载入原件…" />
  }

  switch (cap.renderer) {
    case 'pdf':
      return <PdfPreview blob={content.blob} zoom={zoom} renderFrame={renderFrame} />
    case 'docx':
      return (
        <SinglePage renderFrame={renderFrame}>
          <DocxPreview blob={content.blob} zoom={zoom} />
        </SinglePage>
      )
    case 'xlsx':
      return <XlsxPreview blob={content.blob} zoom={zoom} renderFrame={renderFrame} />
    case 'pptx':
      return (
        <SinglePage renderFrame={renderFrame}>
          <PptxPreview blob={content.blob} zoom={zoom} />
        </SinglePage>
      )
    case 'image':
      return (
        <SinglePage renderFrame={renderFrame}>
          <ImageViewer url={content.url} label={cap.label} zoom={zoom} />
        </SinglePage>
      )
    case 'markdown':
    case 'html':
    case 'text':
    case 'code':
    case 'csv':
      return (
        <SinglePage renderFrame={renderFrame}>
          <TextualPreview blob={content.blob} kind={type} zoom={zoom} />
        </SinglePage>
      )
    default:
      return <FallbackNotice cap={cap} />
  }
}

/** 单页文档（Word / 文本 / 图片 / HTML）：整篇对应第 1 页（Core 页模型） */
function SinglePage({
  renderFrame,
  children
}: {
  renderFrame: (pageNo: number, content: ReactNode) => ReactNode
  children: ReactNode
}): JSX.Element {
  return <>{renderFrame(1, children)}</>
}

/** 多帧 TIFF：每帧对应一页，逐帧向 Core 取 PNG（浏览器无法解码 TIFF） */
function TiffPreview({
  docId,
  pageCount,
  zoom,
  renderFrame
}: {
  docId: string
  pageCount: number
  zoom: number
  renderFrame: (pageNo: number, content: ReactNode) => ReactNode
}): JSX.Element {
  const frames = Math.max(1, pageCount)
  return (
    <div className="doc-render img-render">
      {Array.from({ length: frames }, (_, i) => (
        <div key={i + 1}>{renderFrame(i + 1, <TiffFrame docId={docId} pageNo={i + 1} zoom={zoom} />)}</div>
      ))}
    </div>
  )
}

function TiffFrame({ docId, pageNo, zoom }: { docId: string; pageNo: number; zoom: number }): JSX.Element {
  const content = useDocContent(docId, true, pageNo)
  if (content.status === 'error') {
    return <Notice tone="error" text={`第 ${pageNo} 帧预览失败：${content.error ?? '未知错误'}`} />
  }
  if (!content.url) return <Notice text={`正在解析第 ${pageNo} 帧…`} />
  return (
    <img className="doc-image" src={content.url} alt={`第 ${pageNo} 帧`} style={{ width: `${Math.round(zoom * 100)}%` }} />
  )
}

// ---------------------------------------------------------------------------
// Markdown / 纯文本 / HTML / CSV / 代码
// ---------------------------------------------------------------------------

/** Markdown 正文：委托共享渲染器（token 树 → React 元素；预览内链接一律阻止导航） */
function MarkdownBody({ source, zoom }: { source: string; zoom: number }): JSX.Element {
  return (
    <div className="doc-render md-render" style={{ zoom: String(zoom) }}>
      <MarkdownView source={source} className="md-body" />
    </div>
  )
}

/** 文本类原件：按 UTF-8 → GB18030 解码后交给对应渲染器 */
function TextualPreview({ blob, kind, zoom }: { blob: Blob; kind: string; zoom: number }): JSX.Element {
  const [raw, setRaw] = useState<string | null>(null)
  const [encoding, setEncoding] = useState<'utf-8' | 'gb18030'>('utf-8')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setRaw(null)
    setError(null)
    void decodeBlobText(blob)
      .then((r) => {
        if (cancelled) return
        setEncoding(r.encoding)
        setRaw(r.text)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [blob])

  if (error) return <Notice tone="error" text={`文本解析失败：${error}`} />
  if (raw === null) return <Notice text="正在载入文本…" />

  if (kind === 'html' || kind === 'htm') return <HtmlPreview html={raw} zoom={zoom} />
  if (kind === 'md' || kind === 'markdown') return <MarkdownBody source={raw} zoom={zoom} />
  if (kind === 'csv' || kind === 'tsv') return <CsvPreview blob={blob} kind={kind} />
  if (kind === 'json' || kind === 'xml' || kind === 'yaml' || kind === 'code') {
    return <CodePreview blob={blob} kind={kind} />
  }
  return (
    <div className="doc-render txt-render" style={{ zoom: String(zoom) }}>
      {encoding === 'gb18030' && (
        <div className="csv-meta">已按 GB18030 解码（非 UTF-8 文本）</div>
      )}
      <pre className="txt-body">{raw}</pre>
    </div>
  )
}

/**
 * HTML 原件：经 `lawbuddy-preview://` 沙箱协议渲染（与聊天里的 HTML 预览卡同一安全模型）。
 * iframe 仅开放 `allow-scripts`、不开放同源；Main 侧响应头 CSP 允许内联样式/脚本，
 * 禁止一切外部请求、表单与弹窗。相对资源与外链会被拦截，页面内脚本可正常渲染内容。
 */
function HtmlPreview({ html, zoom }: { html: string; zoom: number }): JSX.Element {
  const [src, setSrc] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    setSrc('')
    setError('')
    const bridge = getBridge()
    if (!bridge) {
      setError('预览桥不可用（非 Electron 环境）')
      return undefined
    }
    bridge.preview
      .register(html)
      .then((url) => {
        if (alive) setSrc(url)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [html])

  if (error) return <Notice tone="error" text={`HTML 预览失败：${error}`} />
  return (
    <div className="doc-render html-render">
      {src ? (
        <iframe
          className="html-frame"
          title="HTML 原件预览"
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          src={src}
          style={{ zoom: String(zoom) }}
        />
      ) : (
        <Notice text="正在准备 HTML 预览…" />
      )}
    </div>
  )
}
