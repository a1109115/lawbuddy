import { Extension, Mark } from '@tiptap/core'
import { Fragment } from '@tiptap/pm/model'
import { Plugin, PluginKey, TextSelection } from '@tiptap/pm/state'
import type { Editor } from '@tiptap/react'
import type { WordNode } from '@lawbuddy/contracts'

/**
 * Word 修订与批注的 TipTap 扩展（与 Core 的 w:ins / w:del / comments 一一对应）。
 *
 * - insertion / deletion：修订标记，导出 DOCX 时写成原生 w:ins / w:del；
 * - comment：批注锚点，导出时生成 commentRangeStart/End 与 word/comments.xml；
 * - RevisionRecorder：开启修订后，自动把新增文字标为插入修订，并拦截
 *   删除/剪切/选区替换，把原文保留为删除修订，供律师逐条接受或拒绝。
 */

export interface RevisionMarkAttrs {
  author?: string | null
  date?: string | null
  revisionId?: string | null
  body?: string | null
}

const REVISION_ATTRIBUTES = {
  author: { default: null },
  date: { default: null },
  revisionId: { default: null }
}

export const InsertionMark = Mark.create({
  name: 'insertion',
  inclusive: false,
  excludes: 'deletion',
  addAttributes() {
    return { ...REVISION_ATTRIBUTES }
  },
  parseHTML() {
    return [{ tag: 'span.rev-insertion' }]
  },
  renderHTML({ mark }) {
    return [
      'span',
      {
        class: 'rev-insertion',
        'data-revision-id': mark.attrs.revisionId ?? '',
        title: `插入修订${mark.attrs.author ? ` · ${mark.attrs.author}` : ''}`
      },
      0
    ]
  }
})

export const DeletionMark = Mark.create({
  name: 'deletion',
  inclusive: false,
  excludes: 'insertion',
  addAttributes() {
    return { ...REVISION_ATTRIBUTES }
  },
  parseHTML() {
    return [{ tag: 'span.rev-deletion' }]
  },
  renderHTML({ mark }) {
    return [
      'span',
      {
        class: 'rev-deletion',
        'data-revision-id': mark.attrs.revisionId ?? '',
        title: `删除修订${mark.attrs.author ? ` · ${mark.attrs.author}` : ''}`
      },
      0
    ]
  }
})

export const CommentMark = Mark.create({
  name: 'comment',
  inclusive: false,
  addAttributes() {
    return {
      author: { default: null },
      date: { default: null },
      revisionId: { default: null },
      body: { default: null }
    }
  },
  parseHTML() {
    return [{ tag: 'span.rev-comment' }]
  },
  renderHTML({ mark }) {
    return [
      'span',
      {
        class: 'rev-comment',
        'data-comment-id': mark.attrs.revisionId ?? '',
        title: mark.attrs.body ?? '批注'
      },
      0
    ]
  }
})

export interface RevisionEntry {
  revisionId: string
  type: 'insertion' | 'deletion'
  author: string
  date: string
  text: string
}

export interface CommentEntry {
  revisionId: string
  author: string
  date: string
  body: string
  text: string
}

/** 收集模型中的修订（按 revisionId 合并连续文本）。 */
export function collectRevisions(model: WordNode): RevisionEntry[] {
  const entries = new Map<string, RevisionEntry>()

  const walk = (node: WordNode): void => {
    for (const child of node.content ?? []) {
      if (child.type === 'text') {
        for (const mark of child.marks ?? []) {
          if (mark.type !== 'insertion' && mark.type !== 'deletion') continue
          const attrs = (mark.attrs ?? {}) as RevisionMarkAttrs
          const revisionId = String(attrs.revisionId ?? '')
          if (!revisionId) continue
          // 同一 revisionId 可同时包含删除与插入（合同建议成对出现），按类型分开展示
          const key = `${mark.type}:${revisionId}`
          const existing = entries.get(key)
          if (existing) {
            existing.text += child.text ?? ''
          } else {
            entries.set(key, {
              revisionId,
              type: mark.type,
              author: String(attrs.author ?? ''),
              date: String(attrs.date ?? ''),
              text: child.text ?? ''
            })
          }
        }
      } else {
        walk(child)
      }
    }
  }

  walk(model)
  return [...entries.values()]
}

/** 收集模型中的批注（按 revisionId 合并连续文本）。 */
export function collectComments(model: WordNode): CommentEntry[] {
  const entries = new Map<string, CommentEntry>()

  const walk = (node: WordNode): void => {
    for (const child of node.content ?? []) {
      if (child.type === 'text') {
        for (const mark of child.marks ?? []) {
          if (mark.type !== 'comment') continue
          const attrs = (mark.attrs ?? {}) as RevisionMarkAttrs
          const revisionId = String(attrs.revisionId ?? '')
          if (!revisionId) continue
          const existing = entries.get(revisionId)
          if (existing) {
            existing.text += child.text ?? ''
          } else {
            entries.set(revisionId, {
              revisionId,
              author: String(attrs.author ?? ''),
              date: String(attrs.date ?? ''),
              body: String(attrs.body ?? ''),
              text: child.text ?? ''
            })
          }
        }
      } else {
        walk(child)
      }
    }
  }

  walk(model)
  return [...entries.values()]
}

/** 当前选区涉及（或光标所在）的修订 id。 */
export function revisionIdsAtSelection(editor: Editor): string[] {
  const ids = new Set<string>()
  const { from, to } = editor.state.selection
  for (const name of ['insertion', 'deletion']) {
    const attrs = editor.getAttributes(name) as RevisionMarkAttrs
    if (attrs.revisionId) ids.add(String(attrs.revisionId))
  }
  editor.state.doc.nodesBetween(from, to, (node) => {
    for (const mark of node.marks) {
      if (mark.type.name !== 'insertion' && mark.type.name !== 'deletion') continue
      if (mark.attrs.revisionId) ids.add(String(mark.attrs.revisionId))
    }
    return true
  })
  return [...ids]
}

function resolveNode(
  node: WordNode,
  accept: boolean,
  ids: string[]
): WordNode {
  const content: WordNode[] = []
  for (const child of node.content ?? []) {
    if (child.type !== 'text') {
      content.push(resolveNode(child, accept, ids))
      continue
    }
    const revision = (child.marks ?? []).find(
      (mark) => mark.type === 'insertion' || mark.type === 'deletion'
    )
    if (revision) {
      const revisionId = String((revision.attrs as RevisionMarkAttrs | undefined)?.revisionId ?? '')
      if (!ids.length || ids.includes(revisionId)) {
        if (revision.type === 'insertion' && !accept) continue
        if (revision.type === 'deletion' && accept) continue
        const marks = (child.marks ?? []).filter(
          (mark) => mark.type !== 'insertion' && mark.type !== 'deletion'
        )
        const next = { ...child }
        if (marks.length) next.marks = marks
        else delete next.marks
        content.push(next)
        continue
      }
    }
    content.push(child)
  }
  return { ...node, content }
}

/** 接受/拒绝修订（纯模型变换；ids 为空表示全部）。 */
export function resolveRevisionsInModel(
  model: WordNode,
  action: 'accept' | 'reject',
  ids: string[] = []
): WordNode {
  return resolveNode(model, action === 'accept', ids)
}

/** 清除批注锚点（正文保留；ids 为空表示全部）。 */
export function removeCommentsInModel(model: WordNode, ids: string[] = []): WordNode {
  const strip = (node: WordNode): WordNode => {
    const content: WordNode[] = []
    for (const child of node.content ?? []) {
      if (child.type !== 'text') {
        content.push(strip(child))
        continue
      }
      const kept = (child.marks ?? []).filter((mark) => {
        if (mark.type !== 'comment') return true
        if (!ids.length) return false
        const revisionId = String((mark.attrs as RevisionMarkAttrs | undefined)?.revisionId ?? '')
        return !ids.includes(revisionId)
      })
      if (kept.length === (child.marks ?? []).length) {
        content.push(child)
        continue
      }
      const next = { ...child }
      if (kept.length) next.marks = kept
      else delete next.marks
      content.push(next)
    }
    return { ...node, content }
  }
  return strip(model)
}

/** 选中某个批注覆盖的正文范围并滚动到可见位置。 */
export function selectCommentRange(editor: Editor, revisionId: string): boolean {
  let from = Number.POSITIVE_INFINITY
  let to = 0
  editor.state.doc.descendants((node, pos) => {
    if (!node.isText) return true
    for (const mark of node.marks) {
      if (mark.type.name !== 'comment') continue
      if (String(mark.attrs.revisionId ?? '') !== revisionId) continue
      from = Math.min(from, pos)
      to = Math.max(to, pos + node.nodeSize)
    }
    return true
  })
  if (!Number.isFinite(from) || to <= from) return false
  editor.chain().focus().setTextSelection({ from, to }).scrollIntoView().run()
  return true
}

export interface RevisionRecorderOptions {
  /** 修订记录是否开启（函数形式，切换时无需重建编辑器） */
  enabled: () => boolean
  author: string
  onNotice: (message: string) => void
}

const TRACK_META = 'lawbuddyRevisionHandled'

/** 修订记录插件：自动标记新增文字，并把删除/替换保留为删除修订。 */
export const RevisionRecorder = Extension.create<RevisionRecorderOptions>({
  name: 'revisionRecorder',

  addOptions() {
    return {
      enabled: () => false,
      author: '本机用户',
      onNotice: () => undefined
    }
  },

  addProseMirrorPlugins() {
    const options = this.options
    const revisionAttrs = (): RevisionMarkAttrs => ({
      author: options.author,
      date: new Date().toISOString(),
      revisionId: `rv-${crypto.randomUUID()}`
    })

    return [
      new Plugin({
        key: new PluginKey('lawbuddyRevisionRecorder'),

        appendTransaction(transactions, oldState, newState) {
          if (!options.enabled()) return null
          if (!transactions.some((transaction) => transaction.docChanged)) return null
          if (transactions.some((transaction) => transaction.getMeta(TRACK_META))) return null
          const start = oldState.doc.content.findDiffStart(newState.doc.content)
          if (start == null) return null
          const diffEnd = oldState.doc.content.findDiffEnd(newState.doc.content)
          const to = diffEnd ? Math.max(start, diffEnd.b) : start
          if (to <= start) return null // 纯删除已由按键拦截处理
          const mark = newState.schema.marks.insertion.create(revisionAttrs())
          const transaction = newState.tr
          transaction.addMark(start, to, mark)
          transaction.setMeta(TRACK_META, true)
          return transaction
        },

        props: {
          handleKeyDown(view, event) {
            if (!options.enabled()) return false
            if (event.key !== 'Backspace' && event.key !== 'Delete') return false
            const { state } = view
            const { from, to, empty } = state.selection
            let start = from
            let end = to
            if (empty) {
              const $from = state.selection.$from
              if (!$from.parent.isTextblock) return false
              if (event.key === 'Backspace') {
                if ($from.parentOffset === 0) {
                  options.onNotice('修订模式下暂不支持跨段删除，可分段标记删除')
                  event.preventDefault()
                  return true
                }
                start = from - 1
              } else {
                if ($from.parentOffset >= $from.parent.content.size) {
                  options.onNotice('修订模式下暂不支持跨段删除，可分段标记删除')
                  event.preventDefault()
                  return true
                }
                end = from + 1
              }
            } else if (!state.selection.$from.sameParent(state.selection.$to)) {
              options.onNotice('修订模式下暂不支持跨段删除，可分段标记删除')
              event.preventDefault()
              return true
            }
            if (!state.doc.textBetween(start, end, '\n', '\n')) return false
            const mark = state.schema.marks.deletion.create(revisionAttrs())
            const transaction = state.tr.addMark(start, end, mark)
            transaction.setSelection(TextSelection.create(transaction.doc, start))
            transaction.setMeta(TRACK_META, true)
            view.dispatch(transaction)
            event.preventDefault()
            return true
          },

          handleTextInput(view, from, to, text) {
            if (!options.enabled() || from === to) return false
            const { state } = view
            if (!state.selection.$from.sameParent(state.selection.$to)) {
              options.onNotice('修订模式下暂不支持跨段替换，可分段处理')
              return true
            }
            const transaction = state.tr
            transaction.addMark(from, to, state.schema.marks.deletion.create(revisionAttrs()))
            transaction.insertText(text, to)
            const insertedTo = to + text.length
            transaction.addMark(to, insertedTo, state.schema.marks.insertion.create(revisionAttrs()))
            transaction.setSelection(TextSelection.create(transaction.doc, insertedTo))
            transaction.setMeta(TRACK_META, true)
            view.dispatch(transaction)
            return true
          },

          handlePaste(view, _event, slice) {
            if (!options.enabled()) return false
            const { state } = view
            const { from, to, empty } = state.selection
            if (empty) return false // 默认粘贴，由 appendTransaction 标记插入
            if (!state.selection.$from.sameParent(state.selection.$to)) {
              options.onNotice('修订模式下暂不支持跨段粘贴替换')
              return true
            }
            try {
              const transaction = state.tr.addMark(
                from, to, state.schema.marks.deletion.create(revisionAttrs())
              )
              transaction.insert(to, slice.content)
              transaction.addMark(
                to, to + slice.content.size,
                state.schema.marks.insertion.create(revisionAttrs())
              )
              transaction.setSelection(TextSelection.create(transaction.doc, to + slice.content.size))
              transaction.setMeta(TRACK_META, true)
              view.dispatch(transaction)
            } catch {
              options.onNotice('修订模式下无法在此位置粘贴该内容')
            }
            return true
          },

          handleDOMEvents: {
            cut(view, event) {
              if (!options.enabled()) return false
              const { state } = view
              const { from, to, empty } = state.selection
              if (empty) return false
              event.preventDefault()
              if (!state.selection.$from.sameParent(state.selection.$to)) {
                options.onNotice('修订模式下暂不支持跨段剪切，可分段标记删除')
                return true
              }
              const text = state.doc.textBetween(from, to, '\n')
              void navigator.clipboard?.writeText(text)
              const transaction = state.tr.addMark(
                from, to, state.schema.marks.deletion.create(revisionAttrs())
              )
              transaction.setSelection(TextSelection.create(transaction.doc, from))
              transaction.setMeta(TRACK_META, true)
              view.dispatch(transaction)
              return true
            }
          }
        }
      })
    ]
  }
})
