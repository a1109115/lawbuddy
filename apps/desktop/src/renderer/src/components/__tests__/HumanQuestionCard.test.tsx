import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { HumanQuestionSet } from '@lawbuddy/contracts'
import { HumanQuestionCard } from '../HumanQuestionCard'

const questionSet: HumanQuestionSet = {
  id: 'qs-1',
  taskId: 'task-1',
  conversationId: 'conv-1',
  title: '请确认辩护方向',
  status: 'pending',
  createdAt: '2026-11-03T10:00:00',
  questions: [
    {
      id: 'q1',
      title: '本案采取哪种辩护方向？',
      kind: 'single_choice',
      reason: '方向不同会改变事实陈述与法律依据的取舍',
      affects: '第一段事实陈述与辩护意见章节',
      options: [
        { id: 'o1', label: '程序违法优先' },
        { id: 'o2', label: '实体不构成犯罪' },
        { id: 'o3', label: '罪轻辩护' }
      ],
      allowOther: true
    }
  ]
}

afterEach(cleanup)

describe('文书决策问题卡键盘操作', () => {
  it('方向键在选项间移动并更新选中态', () => {
    render(
      <HumanQuestionCard msgId="m1" taskId="task-1" questionSet={questionSet} />
    )
    const group = screen.getByRole('radiogroup')
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(3)

    // 未选中时按下方向键选第一项，再按一次前进到第二项
    fireEvent.keyDown(group, { key: 'ArrowDown' })
    expect(screen.getAllByRole('radio')[0].getAttribute('aria-checked')).toBe('true')
    fireEvent.keyDown(group, { key: 'ArrowDown' })
    expect(screen.getAllByRole('radio')[1].getAttribute('aria-checked')).toBe('true')
    // 反向键回到上一项
    fireEvent.keyDown(group, { key: 'ArrowUp' })
    expect(screen.getAllByRole('radio')[0].getAttribute('aria-checked')).toBe('true')
  })

  it('未回答时提交按钮禁用，选择后可提交', () => {
    render(
      <HumanQuestionCard msgId="m1" taskId="task-1" questionSet={questionSet} />
    )
    const submit = screen.getByRole('button', { name: '提交并继续' })
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect((screen.getByRole('button', { name: '提交并继续' }) as HTMLButtonElement).disabled).toBe(
      false
    )
  })

  it('多问题只需回答当前题即可进入下一题', () => {
    const multiple: HumanQuestionSet = {
      ...questionSet,
      questions: [
        questionSet.questions[0],
        {
          ...questionSet.questions[0],
          id: 'q2',
          title: '是否保留备位请求？'
        }
      ]
    }
    render(<HumanQuestionCard msgId="m1" taskId="task-1" questionSet={multiple} />)

    const next = screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement
    expect(next.disabled).toBe(true)
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect(next.disabled).toBe(false)
    fireEvent.click(next)

    expect(screen.getByText('是否保留备位请求？')).toBeTruthy()
    const submit = screen.getByRole('button', { name: '提交并继续' }) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect(submit.disabled).toBe(false)
  })
})
