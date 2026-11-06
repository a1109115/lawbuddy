import { useEffect, useLayoutEffect, useRef, useState, type JSX } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import { Extension } from '@tiptap/core'
import { TableKit } from '@tiptap/extension-table'
import { TextStyleKit } from '@tiptap/extension-text-style'
import TextAlign from '@tiptap/extension-text-align'
import type { WordNode } from '@lawbuddy/contracts'
import { useApp } from '../../state/store'
import { DocToolbar } from './DocToolbar'
import {
  collectComments,
  collectRevisions,
  CommentMark,
  DeletionMark,
  InsertionMark,
  removeCommentsInModel,
  resolveRevisionsInModel,
  RevisionRecorder,
  revisionIdsAtSelection,
  selectCommentRange,
  type CommentEntry,
  type RevisionEntry
} from './revisionMarks'

/**
 * 统一的 Word 模型编辑器（WPS 式编辑体验）。
 *
 * 被三处复用：AI 文件产物直接编辑、合同审查工作副本、文书导出 Word。
 * 只编辑 WordNode 模型（contracts 唯一来源），保存由调用方决定写到哪个目标
 * （artifact.save_editable / word.save_model），因此原件与工作副本边界始终清晰。
 */
export interface WordSaveOutcome {
  ok: boolean
  /** 保存结果说明（如「已保存 · v3」）；失败时用于界面提示 */
  message?: string
}

/** A4 版面宽度（21cm，按 CSS 96dpi 换算）：与 app.css 的 .word-paper 保持一致 */
const WORD_PAGE_WIDTH_PX = 793.7

/**
 * 段落级排版属性（首行缩进 / 段前段后 / 固定行距）。
 *
 * 这些值来自 DOCX 的 pPr（w:ind / w:spacing），由 Core 的 from_docx 写入 WordNode.attrs。
 * 不保留它们会让编辑态丢掉缩进和行距，与 docx-preview 的版式不一致。
 */
const ParagraphFormatting = Extension.create({
  name: 'paragraphFormatting',
  addGlobalAttributes() {
    const parsePt = (value: string): number | null => {
      const parsed = parseFloat(value)
      return Number.isFinite(parsed) ? parsed : null
    }
    return [
      {
        types: ['paragraph', 'heading'],
        attributes: {
          firstLineIndent: {
            default: null,
            parseHTML: (element) => parsePt(element.style.textIndent),
            renderHTML: (attributes) =>
              attributes.firstLineIndent ? { style: `text-indent: ${attributes.firstLineIndent}pt` } : {}
          },
          spaceBefore: {
            default: null,
            parseHTML: (element) => parsePt(element.style.marginTop),
            renderHTML: (attributes) =>
              attributes.spaceBefore ? { style: `margin-top: ${attributes.spaceBefore}pt` } : {}
          },
          spaceAfter: {
            default: null,
            parseHTML: (element) => parsePt(element.style.marginBottom),
            renderHTML: (attributes) =>
              attributes.spaceAfter ? { style: `margin-bottom: ${attributes.spaceAfter}pt` } : {}
          },
          lineSpacing: {
            default: null,
            parseHTML: (element) => parsePt(element.style.lineHeight),
            renderHTML: (attributes) =>
              attributes.lineSpacing ? { style: `line-height: ${attributes.lineSpacing}pt` } : {}
          }
        }
      }
    ]
  }
})

export function WordModelEditor({
  title,
  initialModel,
  warnings,
  liveWarnings,
  save,
  onClose,
  finishLabel = '完成'
}: {
  title: string
  initialModel: WordNode
  warnings?: string[]
  /** 保存过程中新增的告警（如结构变化导致按模型重建） */
  liveWarnings?: string[]
  /** 保存回调：createVersion=true 表示手动保存/完成（形成版本） */
  save: (model: WordNode, createVersion: boolean) => Promise<WordSaveOutcome>
  onClose: () => void
  finishLabel?: string
}): JSX.Element {
  const [status, setStatus] = useState('已保存')
  const [saving, setSaving] = useState(false)
  const [fit, setFit] = useState(1)
  const scrollRef = useRef<HTMLDivElement>(null)
  const [dirty, setDirty] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [trackChanges, setTrackChanges] = useState(false)
  const [revisions, setRevisions] = useState<RevisionEntry[]>(() => collectRevisions(initialModel))
  const [comments, setComments] = useState<CommentEntry[]>(() => collectComments(initialModel))
  const [commentOpen, setCommentOpen] = useState(false)
  const [commentText, setCommentText] = useState('')
  const trackRef = useRef(false)
  const generation = useRef(0)
  const changed = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const queue = useRef(Promise.resolve<boolean>(true))

  const runSave = (createVersion: boolean, model: WordNode): Promise<boolean> => {
    const task = async (): Promise<boolean> => {
      setSaving(true)
      try {
        const r = await save(model, createVersion)
        setStatus(r.ok ? (r.message ?? '已保存') : (r.message ?? '保存失败'))
        return r.ok
      } catch (e) {
        setStatus(`保存失败：${e instanceof Error ? e.message : String(e)}。内容保留在当前窗口，请重试。`)
        return false
      } finally {
        setSaving(false)
      }
    }
    queue.current = queue.current.then(task, task)
    return queue.current
  }

  const editor = useEditor({
    editable: true,
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        link: false,
        code: false,
        codeBlock: false,
        blockquote: false,
        horizontalRule: false
      }),
      TableKit.configure({ table: { resizable: false } }),
      // 字体/字号/文字颜色/背景高亮（与 Core 的 DOCX 序列化一一对应）
      TextStyleKit.configure({ color: {}, backgroundColor: {} }),
      TextAlign.configure({ types: ['heading', 'paragraph'] }),
      ParagraphFormatting,
      InsertionMark,
      DeletionMark,
      CommentMark,
      RevisionRecorder.configure({
        enabled: () => trackRef.current,
        author: '本机用户',
        onNotice: (message) => useApp.getState().toast(message)
      })
    ],
    content: initialModel,
    onUpdate: ({ editor: current }) => {
      const model = current.getJSON() as WordNode
      setRevisions(collectRevisions(model))
      setComments(collectComments(model))
      generation.current += 1
      changed.current = true
      setDirty(true)
      useApp.setState({ wordEditingDirty: true })
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => void autosave(), 2000)
    }
  })

  async function autosave(): Promise<void> {
    if (!editor) return
    const gen = generation.current
    const ok = await runSave(false, editor.getJSON() as WordNode)
    if (ok && generation.current === gen) {
      setDirty(false)
      useApp.setState({ wordEditingDirty: false })
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        if (timer.current) clearTimeout(timer.current)
        const gen = generation.current
        const model = editor?.getJSON() as WordNode | undefined
        if (!model) return
        void runSave(true, model).then((ok) => {
          if (ok && generation.current === gen) {
            changed.current = false
            setDirty(false)
            useApp.setState({ wordEditingDirty: false })
          }
        })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      if (timer.current) clearTimeout(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, save])

  // 编辑纸面按面板宽度等比缩放（与 DocxPreview 的 fit 一致）：
  // 纸张固定为 A4 版心，缩放只改变显示尺寸，不改字号/换行，保证编辑态与预览态观感一致。
  useLayoutEffect(() => {
    const scroll = scrollRef.current
    if (!scroll) return undefined
    const measure = (): void => {
      const style = getComputedStyle(scroll)
      const available =
        scroll.clientWidth -
        parseFloat(style.paddingLeft || '0') -
        parseFloat(style.paddingRight || '0')
      if (available > 0) setFit(Math.min(1, Math.max(0.3, available / WORD_PAGE_WIDTH_PX)))
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(scroll)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!dirty) return undefined
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const finish = async (): Promise<void> => {
    if (timer.current) clearTimeout(timer.current)
    if (dirty || changed.current) {
      const gen = generation.current
      const model = editor?.getJSON() as WordNode | undefined
      if (model) {
        const ok = await runSave(true, model)
        if (!ok) return // 保存失败时保留编辑窗口，避免丢内容
        if (generation.current === gen) {
          changed.current = false
          setDirty(false)
        }
      }
    }
    useApp.setState({ wordEditingDirty: false })
    onClose()
  }

  const persistResolvedModel = (next: WordNode): void => {
    if (!editor) return
    // 取消待触发的自动保存，避免同一改动先手动保存又自动保存成两个修订
    if (timer.current) {
      clearTimeout(timer.current)
      timer.current = null
    }
    editor.commands.setContent(next, { emitUpdate: false })
    setRevisions(collectRevisions(next))
    setComments(collectComments(next))
    setDirty(true)
    useApp.setState({ wordEditingDirty: true })
    void runSave(true, next).then((ok) => {
      if (ok) {
        changed.current = false
        setDirty(false)
        useApp.setState({ wordEditingDirty: false })
      }
    })
  }

  const toggleTrack = (): void => {
    const next = !trackRef.current
    trackRef.current = next
    setTrackChanges(next)
    useApp.getState().toast(
      next ? '修订记录已开启：新增文字记为插入修订，删除保留为删除修订' : '修订记录已关闭'
    )
  }

  const openComment = (): void => {
    if (!editor) return
    if (editor.state.selection.empty) {
      useApp.getState().toast('请先选中要批注的文字')
      return
    }
    setCommentText('')
    setCommentOpen(true)
  }

  const submitComment = (): void => {
    if (!editor) return
    const body = commentText.trim()
    if (!body) return
    editor
      .chain()
      .focus()
      .setMark('comment', {
        author: '本机用户',
        date: new Date().toISOString(),
        revisionId: `c-${crypto.randomUUID()}`,
        body
      })
      .run()
    setCommentOpen(false)
    setCommentText('')
    persistResolvedModel(editor.getJSON() as WordNode)
  }

  const resolve = (action: 'accept' | 'reject', scope: 'all' | 'selection'): void => {
    if (!editor) return
    const ids = scope === 'selection' ? revisionIdsAtSelection(editor) : []
    if (scope === 'selection' && !ids.length) {
      useApp.getState().toast('请先选中要处理的修订')
      return
    }
    const next = resolveRevisionsInModel(editor.getJSON() as WordNode, action, ids)
    persistResolvedModel(next)
    useApp.getState().toast(action === 'accept' ? '已接受修订' : '已拒绝修订')
  }

  const deleteComment = (revisionId: string): void => {
    if (!editor) return
    const next = removeCommentsInModel(editor.getJSON() as WordNode, [revisionId])
    persistResolvedModel(next)
    useApp.getState().toast('批注已删除（正文保留）')
  }

  return (
    <div className={`artifact-editor${expanded ? ' expanded' : ''}`} aria-label={`${title} 编辑`}>
      <div className="artifact-editor-scroll" ref={scrollRef}>
        <div className="word-paper-zoom" style={{ zoom: String(fit) }}>
          <div className="word-paper">
            <EditorContent editor={editor} />
          </div>
        </div>
      </div>

      {comments.length > 0 && (
        <aside className="rev-panel" aria-label="批注列表">
          <div className="rev-panel-head">批注 {comments.length}</div>
          {comments.map((comment) => (
            <div className="rev-comment-card" key={comment.revisionId}>
              <button
                className="rev-comment-quote"
                title="定位到批注文字"
                onClick={() => editor && selectCommentRange(editor, comment.revisionId)}
              >
                {comment.text.slice(0, 80) || '（整段）'}
              </button>
              <div className="rev-comment-body">{comment.body}</div>
              <div className="rev-comment-meta">
                {comment.author || '未知'} · {comment.date.slice(0, 16).replace('T', ' ')}
              </div>
              <button className="btn-ghost" onClick={() => deleteComment(comment.revisionId)}>
                删除批注
              </button>
            </div>
          ))}
        </aside>
      )}

      {commentOpen && (
        <div className="rev-comment-pop" role="dialog" aria-label="添加批注">
          <label className="rev-comment-label" htmlFor="rev-comment-input">
            批注内容
          </label>
          <textarea
            id="rev-comment-input"
            value={commentText}
            autoFocus
            placeholder="写明风险、核实要求或修改理由；该批注会写入 DOCX 原生批注"
            onChange={(event) => setCommentText(event.target.value)}
          />
          <div className="rev-comment-actions">
            <button className="btn-ghost" onClick={() => setCommentOpen(false)}>
              取消
            </button>
            <button className="btn-primary" disabled={!commentText.trim()} onClick={submitComment}>
              添加批注
            </button>
          </div>
        </div>
      )}

      <DocToolbar
        editor={editor}
        status={status}
        saving={saving}
        dirty={dirty}
        expanded={expanded}
        onToggleExpand={() => setExpanded((v) => !v)}
        onFinish={() => void finish()}
        finishLabel={finishLabel}
        revisionCount={revisions.length}
        commentCount={comments.length}
        trackChanges={trackChanges}
        onToggleTrack={toggleTrack}
        onAddComment={openComment}
        onResolve={resolve}
      />
      {warnings?.length || liveWarnings?.length ? (
        <div className="artifact-editor-warn" role="status">
          {[...(warnings ?? []), ...(liveWarnings ?? [])].join('；')}
        </div>
      ) : null}
    </div>
  )
}
