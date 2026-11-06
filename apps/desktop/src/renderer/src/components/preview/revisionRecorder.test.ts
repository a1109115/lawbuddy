import { afterEach, describe, expect, it } from 'vitest'
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import type { WordNode } from '@lawbuddy/contracts'
import {
  CommentMark,
  DeletionMark,
  InsertionMark,
  RevisionRecorder
} from './revisionMarks'

const notices: string[] = []
let enabled = false
let editor: Editor | null = null

function createEditor(text = '甲方应付款。'): Editor {
  editor = new Editor({
    extensions: [
      StarterKit.configure({ link: false }),
      InsertionMark,
      DeletionMark,
      CommentMark,
      RevisionRecorder.configure({
        enabled: () => enabled,
        author: '测试用户',
        onNotice: (message) => notices.push(message)
      })
    ],
    content: {
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text }] }]
    }
  })
  return editor
}

function markedText(current: Editor, type: 'insertion' | 'deletion'): string[] {
  const values: string[] = []
  const walk = (node: WordNode): void => {
    for (const child of node.content ?? []) {
      if (child.type === 'text') {
        if ((child.marks ?? []).some((mark) => mark.type === type)) {
          values.push(child.text ?? '')
        }
      } else {
        walk(child)
      }
    }
  }
  walk(current.getJSON() as WordNode)
  return values
}

function runKey(current: Editor, key: string): boolean {
  const event = {
    key,
    preventDefault: () => undefined
  } as unknown as KeyboardEvent
  let handled = false
  for (const plugin of current.state.plugins) {
    const handler = plugin.props?.handleKeyDown
    if (handler && !handled) handled = Boolean(handler.call(plugin, current.view, event))
  }
  return handled
}

afterEach(() => {
  editor?.destroy()
  editor = null
  enabled = false
  notices.length = 0
})

describe('RevisionRecorder 修订记录', () => {
  it('开启后新增文字自动标记为插入修订', () => {
    const instance = createEditor()
    enabled = true
    instance.commands.setTextSelection(3)
    instance.commands.insertContent('新增')

    expect(markedText(instance, 'insertion').join('')).toContain('新增')
    const marks =
      (instance.getJSON() as WordNode).content?.[0]?.content?.find((node) => node.text === '新增')
        ?.marks ?? []
    expect(marks.some((mark) => mark.type === 'insertion')).toBe(true)
  })

  it('关闭修订时新增文字不带插入标记', () => {
    const instance = createEditor()
    enabled = false
    instance.commands.setTextSelection(3)
    instance.commands.insertContent('新增')
    expect(markedText(instance, 'insertion')).toHaveLength(0)
  })

  it('退格把文字保留为删除修订并移动光标', () => {
    const instance = createEditor()
    enabled = true
    instance.commands.setTextSelection(7) // “甲方应付款。”末尾
    expect(runKey(instance, 'Backspace')).toBe(true)
    expect(markedText(instance, 'deletion').join('')).toBe('。')
    expect(instance.state.selection.from).toBe(6)
  })

  it('选区替换：原文记为删除、新文字记为插入', () => {
    const instance = createEditor()
    enabled = true
    instance.commands.setTextSelection({ from: 1, to: 3 }) // “甲方”
    let handled = false
    for (const plugin of instance.state.plugins) {
      const handler = plugin.props?.handleTextInput
      if (handler && !handled) {
        handled = Boolean(
          handler.call(plugin, instance.view, 1, 3, '乙', () => instance.state.tr)
        )
      }
    }
    expect(handled).toBe(true)
    expect(markedText(instance, 'deletion').join('')).toBe('甲方')
    expect(markedText(instance, 'insertion').join('')).toBe('乙')
  })

  it('跨段删除给出提示而不是静默删除', () => {
    const instance = new Editor({
      extensions: [
        StarterKit.configure({ link: false }),
        InsertionMark,
        DeletionMark,
        CommentMark,
        RevisionRecorder.configure({
          enabled: () => enabled,
          author: '测试用户',
          onNotice: (message) => notices.push(message)
        })
      ],
      content: {
        type: 'doc',
        content: [
          { type: 'paragraph', content: [{ type: 'text', text: '第一段' }] },
          { type: 'paragraph', content: [{ type: 'text', text: '第二段' }] }
        ]
      }
    })
    editor = instance
    enabled = true
    // 选中跨两段的范围（段落边界）
    instance.commands.setTextSelection({ from: 2, to: 8 })
    expect(runKey(instance, 'Backspace')).toBe(true)
    expect(notices.some((message) => message.includes('跨段删除'))).toBe(true)
  })
})
