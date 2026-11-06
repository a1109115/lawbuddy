import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { CaseInfo } from '@lawbuddy/contracts'
import { NewCaseDialog } from '../NewCaseDialog'
import { useApp, type NewCaseForm } from '../../state/store'

const linkedSource: CaseInfo = {
  id: 'case-source',
  workspaceId: 'ws',
  title: '张三诉李四民间借贷纠纷案',
  linkedCaseId: null,
  caseType: 'civil',
  clientName: '张三',
  clientRole: '原告',
  stage: '一审',
  defaultMode: 'legal_review',
  status: 'active',
  pinned: false,
  permissionPreset: 'controlled_external',
  redactionPolicy: 'standard',
  redactionCustomTypes: [],
  createdAt: '2026-10-01T00:00:00Z',
  updatedAt: '2026-10-01T00:00:00Z',
  archivedAt: null
}

const openDialog = (createCase?: (form: NewCaseForm) => Promise<boolean>): void => {
  useApp.setState({
    creatingCase: true,
    mode: 'yj',
    currentCaseId: null,
    cases: [linkedSource],
    ...(createCase ? { createCase } : {})
  })
}

beforeEach(() => {
  openDialog(vi.fn(async (): Promise<boolean> => true))
})
afterEach(() => {
  useApp.setState({ creatingCase: false, cases: [] })
  cleanup()
})

describe('NewCaseDialog 新建任务向导', () => {
  it('未打开时不渲染', () => {
    useApp.setState({ creatingCase: false })
    const { container } = render(<NewCaseDialog />)
    expect(container.querySelector('.modal-overlay')).toBeNull()
  })

  it('打开时渲染对话框与三个任务模式选项', () => {
    render(<NewCaseDialog />)
    expect(screen.getByRole('dialog', { name: '新建任务' })).toBeInTheDocument()
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(3)
    expect(screen.getByRole('radio', { name: '法律阅卷' })).toBeChecked()
  })

  it('点选「文书生成」展示关联案件选择，确认后按所选案件创建', async () => {
    const createCase = vi.fn(async (_form: NewCaseForm): Promise<boolean> => true)
    openDialog(createCase)
    render(<NewCaseDialog />)
    fireEvent.click(screen.getByRole('radio', { name: '文书生成' }))
    // 不再点选即创建：先让用户选择关联案件
    expect(createCase).not.toHaveBeenCalled()
    expect(screen.getByLabelText('关联案件（选填）')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('关联案件（选填）'), { target: { value: 'case-source' } })
    fireEvent.click(screen.getByRole('button', { name: '进入文书生成' }))
    await waitFor(() => expect(createCase).toHaveBeenCalledTimes(1))
    const arg = createCase.mock.calls[0][0]
    expect(arg.defaultMode).toBe('ws')
    expect(arg.title).toBe('')
    expect(arg.linkedCaseId).toBe('case-source')
    expect(arg.clientRequestId).toBeTruthy()
    await waitFor(() => expect(useApp.getState().creatingCase).toBe(false))
  })

  it('文书生成可不关联案件直接创建', async () => {
    const createCase = vi.fn(async (_form: NewCaseForm): Promise<boolean> => true)
    openDialog(createCase)
    render(<NewCaseDialog />)
    fireEvent.click(screen.getByRole('radio', { name: '文书生成' }))
    fireEvent.click(screen.getByRole('button', { name: '进入文书生成' }))
    await waitFor(() => expect(createCase).toHaveBeenCalledTimes(1))
    expect(createCase.mock.calls[0][0].linkedCaseId).toBe('')
  })

  it('阅卷模式必填校验：缺名称/客户时禁用创建，补齐后可提交', async () => {
    const createCase = vi.fn(async (_form: NewCaseForm): Promise<boolean> => true)
    openDialog(createCase)
    render(<NewCaseDialog />)
    const submit = screen.getByRole('button', { name: '创建任务' })
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/任务名称/), { target: { value: '张三诉李四民间借贷纠纷案' } })
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/委托人或客户/), { target: { value: '张三' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)
    await waitFor(() => expect(createCase).toHaveBeenCalledTimes(1))
    expect(createCase.mock.calls[0][0].defaultMode).toBe('yj')
    expect(createCase.mock.calls[0][0].title).toBe('张三诉李四民间借贷纠纷案')
  })

  it('切换到合同审查模式：联动合同类型与审查背景字段', () => {
    render(<NewCaseDialog />)
    fireEvent.click(screen.getByRole('radio', { name: '合同审查' }))
    expect(screen.getByRole('radio', { name: '合同审查' })).toBeChecked()
    expect(screen.getByLabelText('合同类型（选填）')).toBeInTheDocument()
    expect(screen.getByLabelText('审查背景（选填）')).toBeInTheDocument()
    // 阅卷专属字段不再展示
    expect(screen.queryByLabelText('程序阶段')).toBeNull()
  })
})
