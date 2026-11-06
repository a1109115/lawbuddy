/**
 * 预览能力注册表（P0 预览内核）：把「按 kind 硬编码分发」收敛为一份能力声明。
 *
 * 渲染器只负责画；是否分页、能否选择文本、能否定位引证、不可用时如何降级，
 * 都由这里统一回答，避免各个组件各自判断导致体验与提示不一致。
 */

export type PreviewRendererId =
  | 'pdf'
  | 'docx'
  | 'xlsx'
  | 'pptx'
  | 'markdown'
  | 'html'
  | 'text'
  | 'code'
  | 'csv'
  | 'image'
  | 'audio'
  | 'video'
  | 'unsupported'

export interface PreviewCapability {
  renderer: PreviewRendererId
  /** 人类可读的类型名 */
  label: string
  /** 是否需要 Core 内容会话（取本机字节；媒体走 Range 协议不算整份载入） */
  needsContent: boolean
  /** 是否分页（页码 / 跳页 / 缩略图有意义） */
  paginated: boolean
  /** 是否支持文本选择与页内查找 */
  textSelectable: boolean
  /** 是否支持引证定位（原文片段可定位） */
  citationLocatable: boolean
  /** 不可用时的降级说明（null = 原生支持） */
  fallback: string | null
}

const IMAGE_KINDS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'tiff'])
const AUDIO_KINDS = new Set(['mp3', 'wav', 'm4a', 'ogg'])
const VIDEO_KINDS = new Set(['mp4', 'webm'])
const CSV_KINDS = new Set(['csv', 'tsv'])
const CODE_KINDS = new Set(['code', 'json', 'xml', 'yaml', 'yml'])

const CAPS: Record<string, PreviewCapability> = {
  pdf: { renderer: 'pdf', label: 'PDF', needsContent: true, paginated: true, textSelectable: true, citationLocatable: true, fallback: null },
  docx: { renderer: 'docx', label: 'Word', needsContent: true, paginated: true, textSelectable: true, citationLocatable: true, fallback: null },
  xlsx: { renderer: 'xlsx', label: 'Excel', needsContent: true, paginated: true, textSelectable: true, citationLocatable: true, fallback: null },
  pptx: { renderer: 'pptx', label: 'PowerPoint', needsContent: true, paginated: true, textSelectable: false, citationLocatable: false, fallback: null },
  md: { renderer: 'markdown', label: 'Markdown', needsContent: true, paginated: false, textSelectable: true, citationLocatable: true, fallback: null },
  markdown: { renderer: 'markdown', label: 'Markdown', needsContent: true, paginated: false, textSelectable: true, citationLocatable: true, fallback: null },
  html: { renderer: 'html', label: 'HTML', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  htm: { renderer: 'html', label: 'HTML', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  txt: { renderer: 'text', label: '纯文本', needsContent: true, paginated: false, textSelectable: true, citationLocatable: true, fallback: null },
  csv: { renderer: 'csv', label: 'CSV 表格', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  tsv: { renderer: 'csv', label: 'TSV 表格', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  json: { renderer: 'code', label: 'JSON', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  xml: { renderer: 'code', label: 'XML', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  yaml: { renderer: 'code', label: 'YAML', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null },
  code: { renderer: 'code', label: '源码', needsContent: true, paginated: false, textSelectable: true, citationLocatable: false, fallback: null }
}

/** 媒体走 Range 协议，不需要整份内容会话 */
for (const k of AUDIO_KINDS) {
  CAPS[k] = { renderer: 'audio', label: '音频', needsContent: false, paginated: false, textSelectable: false, citationLocatable: false, fallback: null }
}
for (const k of VIDEO_KINDS) {
  CAPS[k] = { renderer: 'video', label: '视频', needsContent: false, paginated: false, textSelectable: false, citationLocatable: false, fallback: null }
}
for (const k of IMAGE_KINDS) {
  CAPS[k] = {
    renderer: 'image',
    label: '图片',
    needsContent: true,
    paginated: k === 'tiff',
    textSelectable: false,
    citationLocatable: false,
    // TIFF 浏览器不能直接解码：由 Core 逐帧转 PNG
    fallback: k === 'tiff' ? 'TIFF 由本地服务逐帧转换为 PNG 后显示' : null
  }
}

/** 旧版 Office 等：本机可提取文本，但不做版式预览（不内置重量级转换器） */
const LEGACY_TEXT_ONLY: Record<string, string> = {
  doc: '旧版 Word（.doc）暂不支持版式预览，当前显示提取文本；建议另存为 .docx 后重新导入。',
  wps: 'WPS 旧格式暂不支持版式预览，当前显示提取文本；建议另存为 .docx 后重新导入。',
  rtf: 'RTF 暂不支持版式预览，当前显示提取文本。',
  odt: 'ODT 暂不支持版式预览，当前显示提取文本。',
  xls: '旧版 Excel（.xls）暂不支持版式预览，当前显示提取文本。',
  ppt: '旧版 PPT（.ppt）暂不支持版式预览，当前显示提取文本。'
}

export function resolvePreview(kind: string): PreviewCapability {
  const k = (kind || '').toLowerCase()
  const known = CAPS[k]
  if (known) return known
  const legacy = LEGACY_TEXT_ONLY[k]
  if (legacy) {
    return {
      renderer: 'text',
      label: k.toUpperCase(),
      needsContent: true,
      paginated: false,
      textSelectable: true,
      citationLocatable: false,
      fallback: legacy
    }
  }
  return {
    renderer: 'unsupported',
    label: (k || '未知').toUpperCase(),
    needsContent: false,
    paginated: false,
    textSelectable: false,
    citationLocatable: false,
    fallback: `已导入的 ${(k || '未知').toUpperCase()} 文件暂不支持预览。`
  }
}

/** 该类型是否有原件预览（其余类型只有提取文本，回退文本视图） */
export function supportsOriginalPreview(kind: string): boolean {
  return resolvePreview(kind).renderer !== 'unsupported'
}

/** 是否音频 */
export function isAudioKind(kind: string): boolean {
  return AUDIO_KINDS.has((kind || '').toLowerCase())
}

/** 是否视频 */
export function isVideoKind(kind: string): boolean {
  return VIDEO_KINDS.has((kind || '').toLowerCase())
}

/** 是否媒体（音视频） */
export function isMediaKind(kind: string): boolean {
  const k = (kind || '').toLowerCase()
  return AUDIO_KINDS.has(k) || VIDEO_KINDS.has(k)
}

/** 是否表格类文本（CSV/TSV） */
export function isCsvKind(kind: string): boolean {
  return CSV_KINDS.has((kind || '').toLowerCase())
}

/** 是否代码/数据类（代码高亮 + 行号） */
export function isCodeKind(kind: string): boolean {
  return CODE_KINDS.has((kind || '').toLowerCase())
}
