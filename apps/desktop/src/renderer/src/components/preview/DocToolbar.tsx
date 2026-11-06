import { useEffect, useState, type JSX } from 'react'
import type { Editor } from '@tiptap/react'
import { Icon } from '../Icon'

/**
 * 统一文档编辑工具栏（WPS 式功能区，Word 模型编辑共用）。
 *
 * 覆盖：撤销重做 / 段落样式 / 字体 / 字号 / 加粗斜体下划线删除线 /
 * 文字颜色 / 背景高亮 / 对齐 / 列表 / 表格增删 / 清除格式 / 字数 /
 * 展开编辑区域 / 保存状态 / 完成。
 *
 * 数据模型只写入 contracts 允许的 mark（bold/italic/underline/strike/textStyle），
 * 由 Core 序列化为原生 DOCX（含 w:color 与 w:shd），不会丢失格式。
 */

const FONTS: Array<{ value: string; label: string }> = [
  { value: 'FangSong', label: '仿宋' },
  { value: 'SimSun', label: '宋体' },
  { value: 'KaiTi', label: '楷体' },
  { value: 'SimHei', label: '黑体' },
  { value: 'Microsoft YaHei', label: '微软雅黑' },
  { value: 'Times New Roman', label: 'Times New Roman' }
]

const SIZES = ['10.5pt', '12pt', '14pt', '16pt', '18pt', '22pt', '24pt', '28pt']

const HIGHLIGHTS: Array<{ value: string; label: string }> = [
  { value: '#FFF3A3', label: '黄' },
  { value: '#FFD8D8', label: '红' },
  { value: '#D8F0D8', label: '绿' },
  { value: '#D8E8FF', label: '蓝' }
]

export interface DocToolbarProps {
  editor: Editor | null
  status: string
  saving: boolean
  dirty: boolean
  expanded: boolean
  onToggleExpand: () => void
  onFinish: () => void
  finishLabel?: string
  /** 待处理修订/批注数量（按钮上直接可见） */
  revisionCount?: number
  commentCount?: number
  trackChanges?: boolean
  onToggleTrack?: () => void
  onAddComment?: () => void
  onResolve?: (action: 'accept' | 'reject', scope: 'all' | 'selection') => void
}

export function DocToolbar({
  editor,
  status,
  saving,
  dirty,
  expanded,
  onToggleExpand,
  onFinish,
  finishLabel = '完成',
  revisionCount = 0,
  commentCount = 0,
  trackChanges = false,
  onToggleTrack,
  onAddComment,
  onResolve
}: DocToolbarProps): JSX.Element {
  // TipTap 的 active 状态不在 React 数据流里：订阅选区/文档变化强制刷新按钮态
  const [, force] = useState(0)
  useEffect(() => {
    if (!editor) return undefined
    const rerender = (): void => force((n) => n + 1)
    editor.on('selectionUpdate', rerender)
    editor.on('transaction', rerender)
    return () => {
      editor.off('selectionUpdate', rerender)
      editor.off('transaction', rerender)
    }
  }, [editor])

  const can = !!editor
  const styleAttrs = (editor?.getAttributes('textStyle') ?? {}) as {
    fontFamily?: string | null
    fontSize?: string | null
    color?: string | null
    backgroundColor?: string | null
  }
  const text = editor?.getText() ?? ''
  const charCount = text.replace(/\s/g, '').length
  const inTable = !!editor?.isActive('table')

  return (
    <div className="edit-toolbar" role="toolbar" aria-label="文档编辑工具栏">
      <button className="et-btn" aria-label="撤销" title="撤销（Ctrl+Z）" disabled={!can} onClick={() => editor?.chain().focus().undo().run()}>
        <Icon id="i-undo" />
      </button>
      <button className="et-btn" aria-label="重做" title="重做（Ctrl+Y）" disabled={!can} onClick={() => editor?.chain().focus().redo().run()}>
        <Icon id="i-redo" />
      </button>
      <span className="et-sep" />

      <select
        className="et-select"
        aria-label="段落格式"
        title="段落格式"
        disabled={!can}
        value={editor?.isActive('heading', { level: 1 }) ? '1' : editor?.isActive('heading', { level: 2 }) ? '2' : editor?.isActive('heading', { level: 3 }) ? '3' : '0'}
        onChange={(e) => {
          const level = Number(e.target.value)
          if (level) editor?.chain().focus().toggleHeading({ level: level as 1 | 2 | 3 }).run()
          else editor?.chain().focus().setParagraph().run()
        }}
      >
        <option value="0">正文</option>
        <option value="1">标题 1</option>
        <option value="2">标题 2</option>
        <option value="3">标题 3</option>
      </select>

      <select
        className="et-select et-font"
        aria-label="字体"
        title="字体"
        disabled={!can}
        value={styleAttrs.fontFamily ?? ''}
        onChange={(e) => {
          if (e.target.value) editor?.chain().focus().setFontFamily(e.target.value).run()
          else editor?.chain().focus().unsetFontFamily().run()
        }}
      >
        <option value="">字体</option>
        {FONTS.map((f) => (
          <option key={f.value} value={f.value}>
            {f.label}
          </option>
        ))}
      </select>

      <select
        className="et-select et-size"
        aria-label="字号"
        title="字号"
        disabled={!can}
        value={styleAttrs.fontSize ?? ''}
        onChange={(e) => {
          if (e.target.value) editor?.chain().focus().setFontSize(e.target.value).run()
          else editor?.chain().focus().unsetFontSize().run()
        }}
      >
        <option value="">字号</option>
        {SIZES.map((size) => (
          <option key={size} value={size}>
            {size.replace('pt', '')}
          </option>
        ))}
      </select>
      <span className="et-sep" />

      <button className={`et-glyph bold${editor?.isActive('bold') ? ' on' : ''}`} aria-label="加粗" aria-pressed={editor?.isActive('bold')} title="加粗（Ctrl+B）" disabled={!can} onClick={() => editor?.chain().focus().toggleBold().run()}>
        B
      </button>
      <button className={`et-glyph italic${editor?.isActive('italic') ? ' on' : ''}`} aria-label="斜体" aria-pressed={editor?.isActive('italic')} title="斜体（Ctrl+I）" disabled={!can} onClick={() => editor?.chain().focus().toggleItalic().run()}>
        I
      </button>
      <button className={`et-glyph underline${editor?.isActive('underline') ? ' on' : ''}`} aria-label="下划线" aria-pressed={editor?.isActive('underline')} title="下划线（Ctrl+U）" disabled={!can} onClick={() => editor?.chain().focus().toggleUnderline().run()}>
        U
      </button>
      <button className={`et-glyph strike${editor?.isActive('strike') ? ' on' : ''}`} aria-label="删除线" aria-pressed={editor?.isActive('strike')} title="删除线" disabled={!can} onClick={() => editor?.chain().focus().toggleStrike().run()}>
        S
      </button>

      <label className="et-color" title="文字颜色（写入 DOCX 字体颜色）">
        <input
          type="color"
          aria-label="文字颜色"
          disabled={!can}
          value={styleAttrs.color ?? '#1f2328'}
          onChange={(e) => editor?.chain().focus().setColor(e.target.value).run()}
        />
        <span style={{ color: styleAttrs.color ?? undefined }}>A</span>
      </label>
      <select
        className="et-select et-size"
        aria-label="背景高亮"
        title="背景高亮（写入 DOCX 文字底纹）"
        disabled={!can}
        value={styleAttrs.backgroundColor ?? ''}
        onChange={(e) => {
          if (e.target.value) editor?.chain().focus().setBackgroundColor(e.target.value).run()
          else editor?.chain().focus().unsetBackgroundColor().run()
        }}
      >
        <option value="">高亮</option>
        {HIGHLIGHTS.map((h) => (
          <option key={h.value} value={h.value}>
            {h.label}
          </option>
        ))}
      </select>
      <span className="et-sep" />

      <button className="et-btn" aria-label="左对齐" title="左对齐" disabled={!can} onClick={() => editor?.chain().focus().setTextAlign('left').run()}>
        <span className="et-align">≡</span>
      </button>
      <button className="et-btn" aria-label="居中" title="居中" disabled={!can} onClick={() => editor?.chain().focus().setTextAlign('center').run()}>
        <span className="et-align center">≡</span>
      </button>
      <button className="et-btn" aria-label="右对齐" title="右对齐" disabled={!can} onClick={() => editor?.chain().focus().setTextAlign('right').run()}>
        <span className="et-align right">≡</span>
      </button>
      <button className="et-btn" aria-label="两端对齐" title="两端对齐" disabled={!can} onClick={() => editor?.chain().focus().setTextAlign('justify').run()}>
        <span className="et-align">☰</span>
      </button>
      <span className="et-sep" />

      <button className={`et-glyph${editor?.isActive('bulletList') ? ' on' : ''}`} aria-label="无序列表" title="无序列表" disabled={!can} onClick={() => editor?.chain().focus().toggleBulletList().run()}>
        •
      </button>
      <button className={`et-glyph${editor?.isActive('orderedList') ? ' on' : ''}`} aria-label="有序列表" title="有序列表" disabled={!can} onClick={() => editor?.chain().focus().toggleOrderedList().run()}>
        1.
      </button>
      <button className="et-btn" aria-label="插入表格" title="插入 2×2 表格" disabled={!can} onClick={() => editor?.chain().focus().insertTable({ rows: 2, cols: 2, withHeaderRow: false }).run()}>
        <span className="et-align table">▦</span>
      </button>
      {inTable && (
        <>
          <button className="et-btn" aria-label="添加行" title="在下方添加一行" onClick={() => editor?.chain().focus().addRowAfter().run()}>
            <span className="et-glyph-sm">行+</span>
          </button>
          <button className="et-btn" aria-label="添加列" title="在右侧添加一列" onClick={() => editor?.chain().focus().addColumnAfter().run()}>
            <span className="et-glyph-sm">列+</span>
          </button>
          <button className="et-btn" aria-label="删除行" title="删除当前行" onClick={() => editor?.chain().focus().deleteRow().run()}>
            <span className="et-glyph-sm">行−</span>
          </button>
          <button className="et-btn" aria-label="删除列" title="删除当前列" onClick={() => editor?.chain().focus().deleteColumn().run()}>
            <span className="et-glyph-sm">列−</span>
          </button>
        </>
      )}
      <span className="et-sep" />

      <button
        className={`et-btn${trackChanges ? ' on' : ''}`}
        aria-label="修订记录"
        aria-pressed={trackChanges}
        title="修订记录：开启后新增文字记为插入修订，删除/剪切/替换记为删除修订"
        onClick={onToggleTrack}
      >
        <span className="et-glyph-sm">修订{revisionCount ? ` ${revisionCount}` : ''}</span>
      </button>
      <button
        className="et-btn"
        aria-label="插入批注"
        title="为选中文字添加批注（写入 DOCX 原生批注）"
        disabled={!can}
        onClick={onAddComment}
      >
        <span className="et-glyph-sm">批注{commentCount ? ` ${commentCount}` : ''}</span>
      </button>
      <select
        className="et-select"
        aria-label="处理修订"
        title="接受或拒绝修订"
        disabled={!can || !revisionCount}
        defaultValue=""
        onChange={(event) => {
          const [action, scope] = event.currentTarget.value.split('-') as [
            'accept' | 'reject',
            'all' | 'selection'
          ]
          event.currentTarget.value = ''
          if (action && onResolve) onResolve(action, scope)
        }}
      >
        <option value="">修订处理</option>
        <option value="accept-selection">接受所选</option>
        <option value="accept-all">接受全部</option>
        <option value="reject-selection">拒绝所选</option>
        <option value="reject-all">拒绝全部</option>
      </select>

      <button className="et-btn" aria-label="清除格式" title="清除格式（保留文字）" disabled={!can} onClick={() => editor?.chain().focus().unsetAllMarks().clearNodes().run()}>
        <Icon id="i-x" />
      </button>
      <button
        className={`et-btn${expanded ? ' on' : ''}`}
        aria-label={expanded ? '退出扩展编辑' : '扩展编辑区域'}
        aria-pressed={expanded}
        title={expanded ? '退出扩展编辑' : '扩展编辑区域（占满右侧面板）'}
        onClick={onToggleExpand}
      >
        <Icon id={expanded ? 'i-shrink' : 'i-expand'} />
      </button>
      <span className="et-sep" />

      <span className="et-count" title="不含空白字符的字数">
        {charCount} 字
      </span>
      <span className={`et-status${dirty ? ' dirty' : ''}`} role="status">
        {saving ? '保存中…' : status}
      </span>
      <button className="et-finish" disabled={saving} onClick={onFinish}>
        {finishLabel}
      </button>
    </div>
  )
}
