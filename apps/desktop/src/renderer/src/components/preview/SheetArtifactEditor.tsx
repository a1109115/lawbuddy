import { useEffect, useRef, useState, type JSX } from 'react'
import type { ArtifactEditable, SheetCellChange, SheetEditSheet } from '@lawbuddy/contracts'
import { getBridge } from '../../services/bridge'
import { useApp } from '../../state/store'
import { Icon } from '../Icon'

/**
 * AI 产物 Excel 直接编辑（轻量网格）：
 * - 只把改动过的单元格增量写回，未改单元格与样式保持原样；
 * - 含图表/图片的文件保存前明确确认（openpyxl 重存会丢失图表）；
 * - 原件/产物边界沿用 ArtifactEditor：只编辑 AI 交付物，不触碰案件卷宗原件。
 */

const UI_ROWS = 200
const UI_COLS = 30

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e))

function colName(index: number): string {
  let n = Math.max(0, index)
  let out = ''
  do {
    out = String.fromCharCode(65 + (n % 26)) + out
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return out
}

function buildCovered(sheet: SheetEditSheet): {
  covered: Set<string>
  spans: Map<string, { rowSpan: number; colSpan: number }>
} {
  const covered = new Set<string>()
  const spans = new Map<string, { rowSpan: number; colSpan: number }>()
  for (const merge of sheet.merges ?? []) {
    spans.set(`${merge.row}:${merge.col}`, { rowSpan: merge.rowSpan, colSpan: merge.colSpan })
    for (let r = merge.row; r < merge.row + merge.rowSpan; r += 1) {
      for (let c = merge.col; c < merge.col + merge.colSpan; c += 1) {
        if (r !== merge.row || c !== merge.col) covered.add(`${r}:${c}`)
      }
    }
  }
  return { covered, spans }
}

export function SheetArtifactEditor({
  info,
  onClose
}: {
  info: ArtifactEditable
  onClose: () => void
}): JSX.Element {
  const sheets = info.sheets ?? []
  const [active, setActive] = useState(0)
  const [edits, setEdits] = useState<Map<string, SheetCellChange>>(new Map())
  const [status, setStatus] = useState('已保存')
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const latest = useRef(edits)
  latest.current = edits

  const save = async (createVersion: boolean): Promise<boolean> => {
    const changes = [...latest.current.values()]
    if (!changes.length) {
      if (createVersion) onClose()
      return true
    }
    const bridge = getBridge()
    if (!bridge) {
      setStatus('保存失败：本地服务未连接')
      return false
    }
    if (info.risky && !window.confirm('该文件含图表/图片，保存后可能丢失，是否继续？')) {
      return false
    }
    setSaving(true)
    try {
      const result = await bridge.artifact.saveSheetEdits({
        artifactId: info.artifactId,
        changes,
        createVersion,
        confirmLoss: !!info.risky
      })
      setStatus(createVersion && result.versionNo != null ? `已保存 · v${result.versionNo}` : '已保存')
      setEdits(new Map())
      setDirty(false)
      useApp.setState({ wordEditingDirty: false })
      if (createVersion) void useApp.getState().loadArtifacts()
      if (result.warnings?.length) useApp.getState().toast(result.warnings.join('；'))
      return true
    } catch (e) {
      setStatus(`保存失败：${errText(e)}。修改保留在当前窗口，请重试。`)
      return false
    } finally {
      setSaving(false)
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void save(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info.artifactId])

  useEffect(() => {
    if (!dirty) return undefined
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const sheet = sheets[active]
  const covered = sheet ? buildCovered(sheet) : { covered: new Set<string>(), spans: new Map() }
  const rows = sheet ? Math.min(sheet.rowCount, UI_ROWS) : 0
  const cols = sheet ? Math.min(sheet.colCount, UI_COLS) : 0

  const editCell = (index: number, row: number, col: number, value: string): void => {
    if (!sheet) return
    const next = new Map(latest.current)
    const key = `${index}:${row}:${col}`
    next.set(key, {
      sheet: sheet.name,
      row: (sheet.startRow ?? 0) + row,
      col: (sheet.startCol ?? 0) + col,
      value
    })
    setEdits(next)
    setDirty(true)
    useApp.setState({ wordEditingDirty: true })
  }

  return (
    <div className="artifact-editor">
      <div className="artifact-editor-scroll">
        <div className="sheet-edit-paper">
          {!sheets.length ? (
            <p className="doc-p" style={{ color: 'var(--ink-300)' }}>
              该工作簿没有可编辑的工作表。
            </p>
          ) : (
            <>
              {sheets.length > 1 && (
                <div className="xls-tabs" role="tablist" aria-label="工作表">
                  {sheets.map((item, index) => (
                    <button
                      key={item.name}
                      type="button"
                      role="tab"
                      aria-selected={index === active}
                      className={`xls-tab${index === active ? ' on' : ''}`}
                      onClick={() => setActive(index)}
                    >
                      {item.name}
                    </button>
                  ))}
                </div>
              )}
              {sheet && (
                <div className="sheet-edit">
                  <div className="sheet-edit-head">
                    <b>{sheet.name}</b>
                    <span className="sheet-edit-chip">只改单元格 · 保留样式</span>
                    {(sheet.freezeRows || sheet.freezeCols) ? (
                      <span className="sheet-edit-chip">冻结 {sheet.freezeRows ?? 0} 行 / {sheet.freezeCols ?? 0} 列</span>
                    ) : null}
                  </div>
                  <div className="sheet-edit-scroll">
                    <table className="sheet-edit-table">
                      <thead>
                        <tr>
                          <th className="sheet-edit-corner" aria-hidden="true" />
                          {Array.from({ length: cols }, (_, c) => (
                            <th key={c}>{colName((sheet.startCol ?? 0) + c)}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {Array.from({ length: rows }, (_, r) => (
                          <tr key={r}>
                            <th className="sheet-edit-rownum">{r + 1}</th>
                            {Array.from({ length: cols }, (_, c) => {
                              if (covered.covered.has(`${r}:${c}`)) return null
                              const span = covered.spans.get(`${r}:${c}`)
                              const key = `${active}:${r}:${c}`
                              const value = latest.current.get(key)?.value ?? sheet.rows[r]?.[c] ?? ''
                              return (
                                <td key={c} rowSpan={span?.rowSpan} colSpan={span?.colSpan}>
                                  <input
                                    className={`sheet-edit-cell${
                                      latest.current.has(key) ? ' changed' : ''
                                    }`}
                                    aria-label={`${sheet.name} ${colName((sheet.startCol ?? 0) + c)}${r + 1}`}
                                    value={value}
                                    onChange={(event) => editCell(active, r, c, event.target.value)}
                                  />
                                </td>
                              )
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {(sheet.rowCount > UI_ROWS || sheet.colCount > UI_COLS) && (
                    <p className="sheet-edit-note">
                      仅显示前 {UI_ROWS} 行 × {UI_COLS} 列；超出范围请使用原文件或导出后编辑。
                    </p>
                  )}
                  {edits.size > 0 && <p className="sheet-edit-note">待保存修改：{edits.size} 个单元格</p>}
                </div>
              )}
            </>
          )}
        </div>
      </div>
      <div className="edit-toolbar" role="toolbar" aria-label="表格编辑工具栏">
        {info.risky && (
          <span className="et-label" style={{ color: 'var(--warn-text)' }} title={info.warnings?.join('；')}>
            <Icon id="i-alert" /> 含图表
          </span>
        )}
        <span className="et-label">Excel 单元格编辑</span>
        <span className="et-sep" />
        <span className="et-count">{edits.size} 处修改</span>
        <span className="et-sep" />
        <span className={`et-status${dirty ? ' dirty' : ''}`} role="status">
          {saving ? '保存中…' : status}
        </span>
        <button className="et-finish" disabled={saving} onClick={() => void save(true)}>
          完成
        </button>
      </div>
      {info.warnings?.length ? (
        <div className="artifact-editor-warn" role="status">
          {info.warnings.join('；')}
        </div>
      ) : null}
    </div>
  )
}
