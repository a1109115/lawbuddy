import JSZip from 'jszip'

/**
 * 读取 XLSX 的版式元数据（冻结窗格 / 自动筛选）。
 *
 * SheetJS 社区版不暴露工作表视图信息，这里直接读 OOXML（zip 内 XML），
 * 只解析只读展示需要的少量字段，不执行公式、不修改文件。
 */

export interface SheetViewMeta {
  /** 冻结的行数（顶部） */
  freezeRows: number
  /** 冻结的列数（左侧） */
  freezeCols: number
  /** 自动筛选范围（0 基索引）；无筛选为 null */
  autoFilter: { startRow: number; startCol: number; endRow: number; endCol: number } | null
}

const EMPTY: SheetViewMeta = { freezeRows: 0, freezeCols: 0, autoFilter: null }

/** A1 引用 → 0 基行列（仅支持单单元格，如 C5） */
function cellRefToRowCol(ref: string): { row: number; col: number } | null {
  const m = /^([A-Z]+)(\d+)$/i.exec(ref.trim())
  if (!m) return null
  let col = 0
  const letters = m[1].toUpperCase()
  for (let i = 0; i < letters.length; i += 1) col = col * 26 + (letters.charCodeAt(i) - 64)
  return { row: Number(m[2]) - 1, col: col - 1 }
}

/** 范围引用（A1:D20 或 A1）→ 0 基范围 */
function rangeRef(ref: string): SheetViewMeta['autoFilter'] {
  const [startRaw, endRaw] = ref.split(':')
  const start = cellRefToRowCol(startRaw)
  if (!start) return null
  const end = endRaw ? cellRefToRowCol(endRaw) : start
  if (!end) return null
  return {
    startRow: Math.min(start.row, end.row),
    startCol: Math.min(start.col, end.col),
    endRow: Math.max(start.row, end.row),
    endCol: Math.max(start.col, end.col)
  }
}

function attr(tag: string, name: string): string | null {
  const m = new RegExp(`${name}\\s*=\\s*"([^"]*)"`, 'i').exec(tag)
  return m ? m[1] : null
}

/**
 * 解析工作簿的 sheet 视图元数据；返回顺序与 workbook.xml 的 sheet 顺序一致。
 * 任一步失败都返回空元数据（预览仍按普通网格展示），不阻断阅读。
 */
export async function readSheetViewMeta(buffer: ArrayBuffer): Promise<SheetViewMeta[]> {
  try {
    const zip = await JSZip.loadAsync(buffer)
    const workbook = await zip.file('xl/workbook.xml')?.async('string')
    const rels = await zip.file('xl/_rels/workbook.xml.rels')?.async('string')
    if (!workbook || !rels) return []

    // rId → 工作表 XML 路径
    const targets = new Map<string, string>()
    for (const tag of rels.match(/<Relationship\b[^>]*>/g) ?? []) {
      const id = attr(tag, 'Id')
      const target = attr(tag, 'Target')
      if (!id || !target) continue
      const path = target.startsWith('/') ? target.slice(1) : `xl/${target.replace(/^\.\//, '')}`
      targets.set(id, path.replace(/\/+/g, '/'))
    }

    // workbook.xml 中 sheet 顺序 → 对应 XML
    const sheetTags = workbook.match(/<sheet\b[^>]*>/g) ?? []
    const orderedPaths: string[] = []
    for (const tag of sheetTags) {
      const rid = attr(tag, 'r:id')
      const path = rid ? targets.get(rid) : null
      if (path) orderedPaths.push(path)
    }

    const out: SheetViewMeta[] = []
    for (const path of orderedPaths) {
      const xml = await zip.file(path)?.async('string')
      if (!xml) {
        out.push(EMPTY)
        continue
      }
      let freezeRows = 0
      let freezeCols = 0
      const pane = /<pane\b[^>]*>/.exec(xml)?.[0]
      if (pane) {
        const state = attr(pane, 'state') ?? ''
        if (state === 'frozen' || state === 'frozenSplit') {
          const y = Number(attr(pane, 'ySplit') ?? 0)
          const x = Number(attr(pane, 'xSplit') ?? 0)
          freezeRows = Number.isFinite(y) ? Math.max(0, Math.floor(y)) : 0
          freezeCols = Number.isFinite(x) ? Math.max(0, Math.floor(x)) : 0
          const topLeft = attr(pane, 'topLeftCell')
          if (topLeft && (!freezeRows || !freezeCols)) {
            const rc = cellRefToRowCol(topLeft)
            if (rc) {
              if (!freezeRows) freezeRows = Math.max(0, rc.row)
              if (!freezeCols) freezeCols = Math.max(0, rc.col)
            }
          }
        }
      }
      const filterRef = /<autoFilter\b[^>]*ref\s*=\s*"([^"]+)"/i.exec(xml)?.[1]
      out.push({
        freezeRows,
        freezeCols,
        autoFilter: filterRef ? rangeRef(filterRef) : null
      })
    }
    return out
  } catch {
    return []
  }
}
