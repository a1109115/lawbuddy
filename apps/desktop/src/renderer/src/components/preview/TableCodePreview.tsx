import { useEffect, useMemo, useState, type JSX } from 'react'
import { decodeBlobText } from '../../services/textDecode'

/** 大表保护：最多渲染的行数，超出给出提示（避免一次插入过多 DOM） */
const ROW_LIMIT = 800

/** 解析分隔符文本：支持引号包裹、双引号转义、字段内换行 */
function parseDelimited(text: string, delimiter: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i += 1
        } else {
          quoted = false
        }
      } else {
        field += ch
      }
      continue
    }
    if (ch === '"') {
      quoted = true
    } else if (ch === delimiter) {
      row.push(field)
      field = ''
    } else if (ch === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
    } else if (ch !== '\r') {
      field += ch
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field)
    rows.push(row)
  }
  return rows.filter((r) => r.some((c) => c.trim().length > 0))
}

/**
 * CSV / TSV 预览：按表格网格渲染（不是把内容塞进纯文本 <pre>）。
 * 首行作为表头；列数按最大行对齐；超出 ROW_LIMIT 提示截断。
 */
export function CsvPreview({ blob, kind }: { blob: Blob; kind: string }): JSX.Element {
  const [text, setText] = useState<string | null>(null)
  const [encoding, setEncoding] = useState<'utf-8' | 'gb18030'>('utf-8')
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    setText(null)
    setError('')
    void decodeBlobText(blob)
      .then((r) => {
        if (!alive) return
        setText(r.text)
        setEncoding(r.encoding)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [blob])

  const model = useMemo(() => {
    if (text === null) return null
    const rows = parseDelimited(text, kind === 'tsv' ? '\t' : ',')
    const colCount = rows.reduce((m, r) => Math.max(m, r.length), 0)
    return {
      header: rows[0] ?? [],
      body: rows.slice(1, ROW_LIMIT + 1),
      colCount,
      total: Math.max(0, rows.length - 1),
      truncated: rows.length - 1 > ROW_LIMIT
    }
  }, [text, kind])

  if (error) return <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>表格解析失败：{error}</p>
  if (!model) return <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>正在解析表格…</p>
  if (model.colCount === 0) return <p className="doc-p doc-indent">文件为空或不含表格数据。</p>

  return (
    <div className="doc-render csv-render">
      <div className="csv-meta">
        {model.colCount} 列 · {model.total} 行
        {encoding === 'gb18030' ? ' · 已按 GB18030 解码' : ''}
        {model.truncated ? ` · 仅显示前 ${ROW_LIMIT} 行` : ''}
      </div>
      <div className="csv-scroll">
        <table className="csv-table">
          <thead>
            <tr>
              <th className="csv-rownum" aria-label="行号" />
              {Array.from({ length: model.colCount }, (_, i) => (
                <th key={i}>{model.header[i] ?? ''}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {model.body.map((r, ri) => (
              <tr key={ri}>
                <th className="csv-rownum">{ri + 1}</th>
                {Array.from({ length: model.colCount }, (_, ci) => (
                  <td key={ci} className="csv-cell">{r[ci] ?? ''}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** JSON / XML / YAML / 源码：带行号的只读文本视图（JSON 自动美化） */
export function CodePreview({ blob, kind }: { blob: Blob; kind: string }): JSX.Element {
  const [text, setText] = useState<string | null>(null)
  const [encoding, setEncoding] = useState<'utf-8' | 'gb18030'>('utf-8')
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    setText(null)
    setError('')
    void decodeBlobText(blob)
      .then((r) => {
        if (!alive) return
        setEncoding(r.encoding)
        let out = r.text
        if (kind === 'json') {
          try {
            out = JSON.stringify(JSON.parse(out), null, 2)
          } catch {
            // 非法 JSON 保持原文，交由用户判断
          }
        }
        setText(out)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [blob, kind])

  if (error) return <p className="doc-p doc-indent" style={{ color: 'var(--red-500)' }}>文本解析失败：{error}</p>
  if (text === null) return <p className="doc-p doc-indent" style={{ color: 'var(--ink-300)' }}>正在载入内容…</p>

  const lines = text.split('\n')
  const MAX_LINES = 5000
  const shown = lines.slice(0, MAX_LINES)
  return (
    <div className="doc-render code-render">
      <div className="csv-meta">
        {lines.length} 行{encoding === 'gb18030' ? ' · 已按 GB18030 解码' : ''}
        {lines.length > MAX_LINES ? ` · 仅显示前 ${MAX_LINES} 行` : ''}
      </div>
      <div className="code-scroll">
        <table className="code-table">
          <tbody>
            {shown.map((line, i) => (
              <tr key={i}>
                <td className="code-ln">{i + 1}</td>
                <td className="code-line">
                  <code>{line || ' '}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
