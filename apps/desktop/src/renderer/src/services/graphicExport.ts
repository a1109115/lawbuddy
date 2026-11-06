/**
 * 可视化产物图形导出：把页面上真实渲染出的 SVG 序列化为文件。
 *
 * 导出的是当前可视化的真实图形（节点、连线、标签），不是把结构化数据转成
 * 文字表格，也不是把 HTML 改扩展名。PNG 由 Renderer 用 canvas 栅格化，
 * 只把最终字节交给 Main 落盘。
 */

/** 在容器内查找第一个 SVG（mermaid 与降级径向图都产出 SVG） */
export function findGraphicSvg(container: HTMLElement | null): SVGSVGElement | null {
  if (!container) return null
  return container.querySelector('svg')
}

/**
 * 序列化 SVG：补 xmlns、显式宽高与白色背景，保证脱离页面后仍可正常打开。
 * 尺寸取真实渲染尺寸（getBoundingClientRect），避免 viewBox 与显示尺寸不一致。
 */
export function serializeSvg(svg: SVGSVGElement): string {
  const rect = svg.getBoundingClientRect()
  const width = Math.max(1, Math.round(rect.width || svg.clientWidth || 800))
  const height = Math.max(1, Math.round(rect.height || svg.clientHeight || 600))
  const clone = svg.cloneNode(true) as SVGSVGElement
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink')
  clone.setAttribute('width', String(width))
  clone.setAttribute('height', String(height))
  if (!clone.getAttribute('viewBox')) clone.setAttribute('viewBox', `0 0 ${width} ${height}`)
  // 白色背景：法律文档导出后直接打印/插入 Word 不会出现透明底
  const bg = clone.ownerDocument.createElementNS('http://www.w3.org/2000/svg', 'rect')
  bg.setAttribute('x', '0')
  bg.setAttribute('y', '0')
  bg.setAttribute('width', '100%')
  bg.setAttribute('height', '100%')
  bg.setAttribute('fill', '#ffffff')
  clone.insertBefore(bg, clone.firstChild)
  return new XMLSerializer().serializeToString(clone)
}

/** SVG 文本 → PNG base64（2 倍分辨率，保证打印清晰） */
export async function svgToPngBase64(svgText: string, scale = 2): Promise<string> {
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svgText)}`
  const img = new Image()
  img.decoding = 'async'
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve()
    img.onerror = () => reject(new Error('图形栅格化失败：SVG 无法载入'))
    img.src = url
  })
  const width = Math.max(1, img.naturalWidth || 800)
  const height = Math.max(1, img.naturalHeight || 600)
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(width * scale)
  canvas.height = Math.round(height * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('图形栅格化失败：画布不可用')
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  const dataUrl = canvas.toDataURL('image/png')
  const base64 = dataUrl.split(',')[1] ?? ''
  if (!base64) throw new Error('图形栅格化失败：未生成 PNG 数据')
  return base64
}
