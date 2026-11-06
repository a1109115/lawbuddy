import { afterEach, describe, expect, it, vi } from 'vitest'
import { useApp } from '../../state/store'

afterEach(() => {
  vi.restoreAllMocks()
  useApp.setState({ docgenDraftId: null, rightView: 'list', rightHide: true })
})

describe('文书草稿与对话并存（升级方案 §10 阶段二）', () => {
  it('打开草稿把文书工作台切到右侧预览，不替换中间区对话', () => {
    useApp.setState({ messages: [], rightHide: true, rightView: 'list' })
    useApp.getState().openDocGen('draft-1')
    const s = useApp.getState()
    expect(s.docgenDraftId).toBe('draft-1')
    expect(s.rightView).toBe('docgen')
    expect(s.rightHide).toBe(false)
    // 中间区仍是对话容器：消息状态不被清空，由 Center 依据 messages 渲染 ChatView
    expect(Array.isArray(s.messages)).toBe(true)
  })

  it('关闭草稿回到文档列表并清空草稿锚点', () => {
    useApp.getState().openDocGen('draft-1')
    useApp.getState().openDocGen(null)
    const s = useApp.getState()
    expect(s.docgenDraftId).toBeNull()
    expect(s.rightView).toBe('list')
  })
})
