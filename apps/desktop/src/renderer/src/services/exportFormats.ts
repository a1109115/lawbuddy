import type { ArtifactExportFormat } from '@lawbuddy/contracts'

/**
 * 产物导出能力（P0）：按产物类型与源文件格式动态生成导出菜单。
 *
 * 不再对每类产物都列同一套格式：
 * - native：原格式导出（Core 同格式字节复制，100% 保真）
 * - convert：经原生渲染器转换（DOCX/PDF/XLSX/PPTX/HTML/TXT 等）
 * - data：结构化数据导出（JSON/CSV）
 * - graphic：可视化产物按当前真实图形导出（SVG/PNG，由 Renderer 序列化）
 */

export type GraphicFormat = 'svg' | 'png'
export type ArtifactExportChoice = ArtifactExportFormat | GraphicFormat

export interface ExportChoice {
  fmt: ArtifactExportChoice
  label: string
  mode: 'native' | 'convert' | 'data' | 'graphic'
  hint: string
}

const LABEL: Record<string, string> = {
  markdown: 'Markdown',
  docx: 'Word',
  pdf: 'PDF',
  xlsx: 'Excel',
  pptx: 'PPT',
  html: 'HTML',
  txt: '纯文本',
  csv: 'CSV',
  json: 'JSON',
  svg: 'SVG 图形',
  png: 'PNG 图片'
}

const EXT_TO_FMT: Record<string, ArtifactExportFormat> = {
  md: 'markdown',
  markdown: 'markdown',
  docx: 'docx',
  pdf: 'pdf',
  xlsx: 'xlsx',
  pptx: 'pptx',
  html: 'html',
  htm: 'html',
  txt: 'txt',
  csv: 'csv',
  json: 'json'
}

/** 文件类产物的可转换目标（不含原格式；图像/音视频不提供转换，避免生成伪文件） */
const CONVERTIBLE: Record<string, ArtifactExportFormat[]> = {
  docx: ['pdf', 'markdown', 'html', 'txt'],
  pdf: ['docx', 'markdown', 'html', 'txt'],
  xlsx: ['pdf', 'markdown', 'html', 'txt'],
  pptx: ['pdf', 'markdown', 'html', 'txt'],
  md: ['docx', 'pdf', 'html', 'txt'],
  markdown: ['docx', 'pdf', 'html', 'txt'],
  html: ['markdown', 'docx', 'pdf', 'txt'],
  htm: ['markdown', 'docx', 'pdf', 'txt'],
  txt: ['markdown', 'docx', 'pdf', 'html'],
  csv: ['markdown', 'docx', 'pdf', 'html', 'txt'],
  json: ['txt', 'markdown'],
  xml: ['txt', 'markdown'],
  yaml: ['txt', 'markdown'],
  yml: ['txt', 'markdown'],
  code: ['txt', 'markdown']
}

/** 结构化产物的可导出格式（xlsx/pptx 由块序列渲染，天然支持表格/要点） */
const STRUCTURED_FORMATS: ArtifactExportFormat[] = [
  'markdown',
  'docx',
  'pdf',
  'html',
  'txt',
  'xlsx',
  'pptx',
  'json'
]

const titleExt = (title: string): string => {
  const t = (title || '').toLowerCase()
  const idx = t.lastIndexOf('.')
  return idx > 0 && idx < t.length - 1 ? t.slice(idx + 1) : ''
}

function option(fmt: ArtifactExportChoice, mode: ExportChoice['mode'], hint: string): ExportChoice {
  const suffix = fmt === 'markdown' ? 'md' : fmt
  return {
    fmt,
    label: `${LABEL[fmt] ?? String(fmt).toUpperCase()}（.${suffix}）`,
    mode,
    hint
  }
}

/**
 * 生成某产物的导出选项。file 类产物按源扩展名给「原格式 + 可转换格式」；
 * 结构化产物按 body 结构给原生渲染格式，可视化产物额外给 SVG/PNG。
 */
export function exportOptionsFor(artifact: { kind: string; title: string }): ExportChoice[] {
  const isFile = artifact.kind === 'file'
  const ext = titleExt(artifact.title)

  if (isFile && ext) {
    const nativeFmt = EXT_TO_FMT[ext] ?? (ext as ArtifactExportFormat)
    const out: ExportChoice[] = [
      option(nativeFmt, 'native', '按当前版本原文件导出，字节级保真')
    ]
    for (const fmt of CONVERTIBLE[ext] ?? []) {
      out.push(option(fmt, 'convert', '经原生渲染器转换，保留正文结构与引证文本'))
    }
    return out
  }

  const out = STRUCTURED_FORMATS.map((fmt) =>
    fmt === 'json'
      ? option(fmt, 'data', '导出结构化数据（便于二次处理与复核）')
      : option(fmt, 'convert', '经原生渲染器生成，保留章节、表格与引证文本')
  )
  // 表格型产物额外提供 CSV（无表格数据时 Core 会明确拒绝，不会生成空表）
  if (['viz', 'evidence', 'points'].includes(artifact.kind)) {
    out.push(option('csv', 'data', '仅导出表格数据（无表格时明确提示）'))
  }
  return out
}

/** 可视化产物是否提供图形导出（时间线/关系图/流程图按当前真实图形导出） */
export function supportsGraphicExport(artifact: { kind: string; hasVizBody?: boolean }): boolean {
  return artifact.kind === 'viz' || !!artifact.hasVizBody
}

export function graphicOptions(): ExportChoice[] {
  return [
    option('svg', 'graphic', '按当前真实图形导出矢量文件，可继续编辑'),
    option('png', 'graphic', '按当前真实图形导出位图，便于直接插入文档')
  ]
}
