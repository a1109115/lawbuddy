import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { DocGenBody, LawBuddyBridge } from '@lawbuddy/contracts'
import { DocGenMessageView } from '../DocGenView'
import { useApp } from '../../state/store'

/** 最小桥：只提供消息卡需要的草稿读取（版本锚点断言的数据源） */
function installBridge(currentVersionNo: number): void {
  const getDraft = vi.fn(async () => ({
    id: 'draft-1',
    currentVersionNo,
    preview: null
  }))
  ;(window as unknown as { lawbuddy: Partial<LawBuddyBridge> }).lawbuddy = {
    docgen: { getDraft } as unknown as LawBuddyBridge['docgen']
  }
}

const baseBody: DocGenBody = {
  draftId: 'draft-1',
  docType: '民事起诉状',
  outline: null,
  outlineConfirmed: true,
  versionNo: 2,
  slots: [],
  missingFields: []
}

beforeEach(() => {
  useApp.setState({ mode: 'ws', artifacts: [], toasts: [] })
})

afterEach(() => {
  cleanup()
  delete (window as unknown as { lawbuddy?: unknown }).lawbuddy
})

describe('文书生成消息卡的版本锚点', () => {
  it('当前稿领先于本条结果时提示版本已推进，并给出两个版本号', async () => {
    installBridge(4)
    render(<DocGenMessageView body={baseBody} />)
    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toContain('v4')
    })
    expect(screen.getByRole('status').textContent).toContain('v2')
    expect(screen.getByRole('status').textContent).toContain('版本历史')
  })

  it('本条结果即当前稿时不提示版本领先', async () => {
    installBridge(2)
    render(<DocGenMessageView body={baseBody} />)
    await waitFor(() => expect(screen.getAllByText(/v2/).length).toBeGreaterThan(0))
    expect(screen.queryByRole('status')).toBeNull()
  })
})
