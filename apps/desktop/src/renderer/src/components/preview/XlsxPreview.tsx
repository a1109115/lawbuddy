import { useEffect, useRef, useState, type CSSProperties, type JSX, type ReactNode } from 'react'
import { readSheetViewMeta, type SheetViewMeta } from '../../services/xlsxMeta'

/**
 * Excel（XLSX）原件预览（REQ-DOC-014）：SheetJS 解析 + 表格网格还原。
 *
 * 还原列宽、行高、合并单元格、单元格显示值（按数字格式渲染的文本）与单元格
 * 样式（字体粗斜/下划线/颜色、填充色、对齐，接近 Excel 原生观感）；
 * 每个工作表对应一个页容器（Core 的页模型即「一 sheet 一页」），页批注与文本视图一致。
 * 只读解析，不执行公式与宏（REQ-DOC-001）。
 */

/** 大表保护：单表最多渲染的行数（超出部分给出提示，避免一次插入过多 DOM） */
const ROW_LIMIT = 2000

interface GridCell {
  text: string
  rowSpan: number
  colSpan: number
  style?: CSSProperties
}

interface SheetModel {
  name: string
  rowCount: number
  colCount: number
  colWidths: number[]
  /** 行高（px，来自原件行设置；未设置为 undefined） */
  rowHeights: Array<number | undefined>
  /** null = 被合并单元格覆盖的位置 */
  cells: Array<Array<GridCell | null>>
  truncated: boolean
  /** 工作表视图（冻结窗格 / 自动筛选） */
  meta: SheetViewMeta
}

/** SheetJS 单元格样式（cellStyles 读取；字段均为可选，不同来源覆盖度不同） */
interface RawCellStyle {
  font?: { bold?: boolean; italic?: boolean; underline?: boolean; strike?: boolean; color?: { rgb?: string } }
  fill?: { fgColor?: { rgb?: string }; patternType?: string }
  alignment?: { horizontal?: string; vertical?: string; wrapText?: boolean }
}

/** AARRGGBB / RRGGBB → #RRGGBB（透明或主题色无 rgb 时不着色） */
function hexColor(rgb: string | undefined): string | undefined {
  if (!rgb || !/^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$/.test(rgb)) return undefined
  if (rgb.length === 8 && rgb.slice(0, 2).toLowerCase() === '00') return undefined
  return `#${rgb.slice(-6)}`
}

/** 单元格样式 → 内联 CSS（缺项即省略，逼近 Excel 显示效果） */
function cellStyle(s: RawCellStyle | undefined): CSSProperties | undefined {
  if (!s) return undefined
  const st: CSSProperties = {}
  if (s.font?.bold) st.fontWeight = 700
  if (s.font?.italic) st.fontStyle = 'italic'
  const deco = [s.font?.underline ? 'underline' : '', s.font?.strike ? 'line-through' : '']
    .filter(Boolean)
    .join(' ')
  if (deco) st.textDecoration = deco
  const fontColor = hexColor(s.font?.color?.rgb)
  if (fontColor) st.color = fontColor
  const fill = s.fill?.patternType && s.fill.patternType !== 'none' ? hexColor(s.fill.fgColor?.rgb) : undefined
  if (fill) st.backgroundColor = fill
  const h = s.alignment?.horizontal
  if (h && h !== 'general') st.textAlign = h as CSSProperties['textAlign']
  const v = s.alignment?.vertical
  if (v) st.verticalAlign = v === 'center' ? 'middle' : v
  return Object.keys(st).length > 0 ? st : undefined
}

/** 列序号 → 列名（0 → A，26 → AA） */
function colName(index: number): string {
  let n = index
  let out = ''
  do {
    out = String.fromCharCode(65 + (n % 26)) + out
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return out
}

async function parseSheets(blob: Blob): Promise<SheetModel[]> {
  const XLSX = await import('xlsx')
  const buffer = await blob.arrayBuffer()
  const wb = XLSX.read(new Uint8Array(buffer), { type: 'array', cellDates: true, cellStyles: true })
  const metas = await readSheetViewMeta(buffer)
  return wb.SheetNames.map((name, sheetIndex) => {
    const ws = wb.Sheets[name]
    const ref = ws?.['!ref']
    const meta = metas[sheetIndex] ?? { freezeRows: 0, freezeCols: 0, autoFilter: null }
    if (!ws || !ref) {
      return {
        name,
        rowCount: 0,
        colCount: 0,
        colWidths: [],
        rowHeights: [],
        cells: [],
        truncated: false,
        meta
      }
    }
    const range = XLSX.utils.decode_range(ref)
    const rowCount = Math.min(range.e.r, range.s.r + ROW_LIMIT - 1) - range.s.r + 1
    const colCount = range.e.c - range.s.c + 1
    const read = (r: number, c: number): string => {
      const cell = ws[XLSX.utils.encode_cell({ r, c })] as { w?: unknown; v?: unknown } | undefined
      if (!cell) return ''
      // w = 按数字格式渲染的显示文本；无格式信息时退回原始值
      if (typeof cell.w === 'string') return cell.w
      const v = cell.v
      if (v == null) return ''
      if (v instanceof Date) return v.toISOString().slice(0, 10)
      return String(v)
    }
    const cells: Array<Array<GridCell | null>> = []
    for (let r = 0; r < rowCount; r += 1) {
      const row: Array<GridCell | null> = []
      for (let c = 0; c < colCount; c += 1) {
        const raw = ws[XLSX.utils.encode_cell({ r: range.s.r + r, c: range.s.c + c })] as
          | { s?: RawCellStyle }
          | undefined
        row.push({
          text: read(range.s.r + r, range.s.c + c),
          rowSpan: 1,
          colSpan: 1,
          style: cellStyle(raw?.s)
        })
      }
      cells.push(row)
    }
    for (const m of ws['!merges'] ?? []) {
      const r0 = m.s.r - range.s.r
      const c0 = m.s.c - range.s.c
      const r1 = Math.min(m.e.r - range.s.r, rowCount - 1)
      const c1 = Math.min(m.e.c - range.s.c, colCount - 1)
      if (r0 < 0 || c0 < 0 || r0 > r1 || c0 > c1) continue
      const anchor = cells[r0]?.[c0]
      if (!anchor) continue
      anchor.rowSpan = r1 - r0 + 1
      anchor.colSpan = c1 - c0 + 1
      for (let r = r0; r <= r1; r += 1) {
        for (let c = c0; c <= c1; c += 1) {
          if (r !== r0 || c !== c0) cells[r][c] = null
        }
      }
    }
    const cols = (ws['!cols'] ?? []) as Array<{ wch?: number; wpx?: number } | undefined>
    const colWidths: number[] = []
    for (let c = 0; c < colCount; c += 1) {
      const spec = cols[range.s.c + c]
      const px = spec?.wpx ?? (spec?.wch != null ? Math.round(spec.wch * 7 + 5) : null)
      colWidths.push(Math.min(360, Math.max(46, px ?? 88)))
    }
    const rowsSpec = (ws['!rows'] ?? []) as Array<{ hpx?: number; hpt?: number } | undefined>
    const rowHeights: Array<number | undefined> = []
    for (let r = 0; r < rowCount; r += 1) {
      const spec = rowsSpec[range.s.r + r]
      rowHeights.push(spec?.hpx ?? (spec?.hpt != null ? Math.round((spec.hpt * 4) / 3) : undefined))
    }
    return {
      name,
      rowCount,
      colCount,
      colWidths,
      rowHeights,
      cells,
      truncated: range.e.r - range.s.r + 1 > rowCount,
      meta
    }
  })
}

/** 冻结窗格布局：计算冻结行列的 sticky 偏移（表头高 26px、行号宽 40px） */
function frozenLayout(
  sheet: SheetModel,
  hasRowMerges: boolean,
  hasColMerges: boolean
): { freezeRows: number; freezeCols: number; rowTop: number[]; colLeft: number[] } {
  const ROW_HEADER_W = 40
  const HEADER_H = 26
  const DEFAULT_ROW_H = 21
  // 合并单元格跨冻结边界时撤掉该方向的冻结，避免 sticky 与 rowSpan 冲突导致错位
  const freezeRows = hasRowMerges ? 0 : Math.min(sheet.meta.freezeRows, sheet.rowCount)
  const freezeCols = hasColMerges ? 0 : Math.min(sheet.meta.freezeCols, sheet.colCount)
  const rowTop: number[] = []
  let top = HEADER_H
  for (let r = 0; r < freezeRows; r += 1) {
    rowTop.push(top)
    top += sheet.rowHeights[r] ?? DEFAULT_ROW_H
  }
  const colLeft: number[] = []
  let left = ROW_HEADER_W
  for (let c = 0; c < freezeCols; c += 1) {
    colLeft.push(left)
    left += sheet.colWidths[c] ?? 88
  }
  return { freezeRows, freezeCols, rowTop, colLeft }
}

export function XlsxPreview({
  blob,
  zoom,
  renderFrame
}: {
  blob: Blob
  zoom: number
  /** 每个工作表一个页容器（PageFrame：页码 / 页批注） */
  renderFrame: (pageNo: number, content: ReactNode) => ReactNode
}): JSX.Element {
  const [sheets, setSheets] = useState<SheetModel[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    setSheets(null)
    setError(null)
    void parseSheets(blob)
      .then((parsed) => {
        if (!cancelled) setSheets(parsed)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [blob])

  if (error) {
    return (
      <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>
        Excel 原件预览失败：{error}
      </p>
    )
  }
  if (!sheets) {
    return (
      <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
        正在解析 Excel 原件…
      </p>
    )
  }

  // 工作表切换（WPS 式标签）：每 sheet 对应一个页容器，点击滚动到对应页
  const jumpSheet = (index: number): void => {
    const paper = rootRef.current?.closest('.paper')
    const target = paper?.querySelector<HTMLElement>(`.doc-page[data-page="${index + 1}"]`)
    target?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }

  return (
    <div className="doc-render xls-render" ref={rootRef}>
      {sheets.length > 1 && (
        <div className="xls-tabs" role="tablist" aria-label="工作表">
          {sheets.map((sheet, i) => (
            <button
              key={sheet.name}
              type="button"
              role="tab"
              className="xls-tab"
              title={`切换到工作表「${sheet.name}」`}
              onClick={() => jumpSheet(i)}
            >
              {sheet.name}
            </button>
          ))}
        </div>
      )}
      {sheets.map((sheet, i) => {
        const hasRowMerges = sheet.cells.some((row) => row.some((c) => c && c.rowSpan > 1))
        const hasColMerges = sheet.cells.some((row) => row.some((c) => c && c.colSpan > 1))
        const layout = frozenLayout(sheet, hasRowMerges, hasColMerges)
        const filter = sheet.meta.autoFilter
        const frozenLabel = layout.freezeRows || layout.freezeCols
          ? `冻结 ${layout.freezeRows} 行 / ${layout.freezeCols} 列`
          : ''
        return (
          <div key={sheet.name}>
            {renderFrame(
              i + 1,
              <div className="xls-sheet" style={{ zoom: String(zoom) }}>
                <div className="xls-sheet-name">
                  <span>{sheet.name}</span>
                  {frozenLabel && <span className="xls-view-chip" title="按原件冻结窗格显示">{frozenLabel}</span>}
                  {filter && <span className="xls-view-chip" title="原件设置了自动筛选（预览只读，不改数据）">含筛选</span>}
                </div>
                {sheet.rowCount === 0 ? (
                  <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>
                    该工作表无内容。
                  </p>
                ) : (
                  <table className="xls-table">
                    <thead>
                      <tr>
                        <th
                          className="xls-corner"
                          aria-hidden="true"
                          style={{ position: 'sticky', left: 0, top: 0, zIndex: 6 }}
                        />
                        {sheet.colWidths.map((w, c) => {
                          const frozenCol = c < layout.freezeCols
                          const filtered = !!filter && c >= filter.startCol && c <= filter.endCol
                          return (
                            <th
                              key={c}
                              scope="col"
                              style={{
                                width: `${w}px`,
                                ...(frozenCol
                                  ? {
                                      position: 'sticky',
                                      left: layout.colLeft[c],
                                      zIndex: 5,
                                      background: '#eef2f8'
                                    }
                                  : {})
                              }}
                            >
                              {colName(c)}
                              {filtered && (
                                <span className="xls-filter" title="原件设置了自动筛选（只读）">
                                  ▾
                                </span>
                              )}
                            </th>
                          )
                        })}
                      </tr>
                    </thead>
                    <tbody>
                      {sheet.cells.map((row, r) => {
                        const frozenRow = r < layout.freezeRows
                        return (
                          <tr key={r} style={sheet.rowHeights[r] != null ? { height: `${sheet.rowHeights[r]}px` } : undefined}>
                            <th
                              className="xls-rownum"
                              scope="row"
                              style={{
                                position: 'sticky',
                                left: 0,
                                zIndex: frozenRow ? 5 : 3,
                                ...(frozenRow ? { top: layout.rowTop[r] } : {})
                              }}
                            >
                              {r + 1}
                            </th>
                            {row.map((cell, c) => {
                              if (!cell) return null
                              const frozenCol = c < layout.freezeCols
                              const sticky: CSSProperties = {}
                              if (frozenCol) {
                                sticky.position = 'sticky'
                                sticky.left = layout.colLeft[c]
                                sticky.zIndex = frozenRow ? 6 : 4
                              }
                              if (frozenRow) {
                                sticky.position = 'sticky'
                                sticky.top = layout.rowTop[r]
                                sticky.zIndex = frozenCol ? 6 : 4
                              }
                              if (frozenCol || frozenRow) {
                                // 冻结单元格必须有不透明底色，否则滚动时下方内容会透出
                                sticky.background = (cell.style?.backgroundColor as string) ?? '#fff'
                              }
                              return (
                                <td
                                  key={c}
                                  rowSpan={cell.rowSpan}
                                  colSpan={cell.colSpan}
                                  style={{ ...cell.style, ...sticky }}
                                >
                                  {cell.text}
                                </td>
                              )
                            })}
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
                {sheet.truncated && (
                  <p className="xls-note">
                    表格较大，仅显示前 {ROW_LIMIT} 行（原件只读不变）；完整内容可在文本视图中查看。
                  </p>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}