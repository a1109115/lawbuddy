import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ModeTabs } from '../ModeTabs'
import { useApp } from '../../state/store'

const resetState = (): void => {
  useApp.setState({ mode: 'yj', currentCaseId: null, toasts: [] })
}

beforeEach(resetState)
afterEach(cleanup)

describe('ModeTabs 模式切换与任务内锁定', () => {
  it('无进行中任务时渲染三个模式页签，当前模式标记选中', () => {
    render(<ModeTabs />)
    const tabs = screen.getAllByRole('tab')
    expect(tabs).toHaveLength(3)
    expect(screen.getByRole('tab', { selected: true }).textContent).toContain('法律阅卷')
    const unselected = screen.getAllByRole('tab', { selected: false })
    expect(unselected).toHaveLength(2)
    expect(unselected.some((t) => t.textContent?.includes('合同审查'))).toBe(true)
    expect(unselected.some((t) => t.textContent?.includes('文书生成'))).toBe(true)
  })

  it('任务进行中不渲染模式切换控件，防止任务内改模式', () => {
    useApp.setState({ currentCaseId: 'case-lock' })
    const { container } = render(<ModeTabs />)
    expect(container.querySelector('.mode-tabs')).toBeNull()
    expect(screen.queryByRole('tab')).toBeNull()
  })

  it('文书生成模式不渲染顶部模式切换控件', () => {
    useApp.setState({ mode: 'ws' })
    const { container } = render(<ModeTabs />)
    expect(container.querySelector('.mode-tabs')).toBeNull()
    expect(screen.queryByRole('tab')).toBeNull()
  })

  it('无任务时点击页签切换模式', () => {
    render(<ModeTabs />)
    fireEvent.click(screen.getByRole('tab', { name: /合同审查/ }))
    expect(useApp.getState().mode).toBe('ht')
  })

  it('任务内调用 setMode 被拒绝并提示模式已锁定', () => {
    useApp.setState({ currentCaseId: 'case-lock' })
    useApp.getState().setMode('ws')
    expect(useApp.getState().mode).toBe('yj')
    expect(useApp.getState().toasts.some((t) => t.text.includes('模式已锁定'))).toBe(true)
  })
})
