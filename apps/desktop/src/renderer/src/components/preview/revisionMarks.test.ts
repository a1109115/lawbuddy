import { describe, expect, it } from 'vitest'
import type { WordNode } from '@lawbuddy/contracts'
import {
  collectComments,
  collectRevisions,
  removeCommentsInModel,
  resolveRevisionsInModel
} from './revisionMarks'

const DATE = '2026-10-28T09:30:00Z'

function model(): WordNode {
  return {
    type: 'doc',
    content: [
      {
        type: 'paragraph',
        content: [
          { type: 'text', text: '甲方应' },
          {
            type: 'text',
            text: '于2026年1月1日前',
            marks: [{ type: 'deletion', attrs: { author: 'LawBuddy', date: DATE, revisionId: 'rv-1' } }]
          },
          {
            type: 'text',
            text: '在验收后十日内',
            marks: [
              { type: 'insertion', attrs: { author: 'LawBuddy', date: DATE, revisionId: 'rv-1' } },
              {
                type: 'comment',
                attrs: { author: 'LawBuddy', date: DATE, revisionId: 'c-1', body: '付款期限需核实' }
              }
            ]
          },
          { type: 'text', text: '支付价款。' }
        ]
      }
    ]
  }
}

function texts(node: WordNode): string[] {
  const values: string[] = []
  const walk = (current: WordNode): void => {
    for (const child of current.content ?? []) {
      if (child.type === 'text') values.push(child.text ?? '')
      else walk(child)
    }
  }
  walk(node)
  return values
}

describe('revisionMarks 模型变换', () => {
  it('按 revisionId 汇总修订与批注', () => {
    const revisions = collectRevisions(model())
    expect(revisions).toHaveLength(2)
    expect(revisions.find((entry) => entry.type === 'deletion')).toMatchObject({
      revisionId: 'rv-1',
      text: '于2026年1月1日前'
    })
    expect(revisions.find((entry) => entry.type === 'insertion')).toMatchObject({
      revisionId: 'rv-1',
      text: '在验收后十日内'
    })

    const comments = collectComments(model())
    expect(comments).toHaveLength(1)
    expect(comments[0]).toMatchObject({
      revisionId: 'c-1',
      body: '付款期限需核实',
      text: '在验收后十日内'
    })
  })

  it('接受修订：删除原文、保留新增文本', () => {
    const accepted = resolveRevisionsInModel(model(), 'accept')
    expect(texts(accepted)).not.toContain('于2026年1月1日前')
    expect(texts(accepted)).toContain('在验收后十日内')
    const marks = collectRevisions(accepted)
    expect(marks).toHaveLength(0)
  })

  it('拒绝修订：保留原文、移除新增文本', () => {
    const rejected = resolveRevisionsInModel(model(), 'reject')
    expect(texts(rejected)).toContain('于2026年1月1日前')
    expect(texts(rejected)).not.toContain('在验收后十日内')
    expect(collectRevisions(rejected)).toHaveLength(0)
  })

  it('指定 revisionId 只处理对应修订', () => {
    const other = model()
    const paragraph = other.content?.[0]
    paragraph?.content?.push({
      type: 'text',
      text: '（已插入）',
      marks: [{ type: 'insertion', attrs: { author: '张律师', date: DATE, revisionId: 'rv-2' } }]
    })
    const accepted = resolveRevisionsInModel(other, 'accept', ['rv-1'])
    expect(texts(accepted)).not.toContain('于2026年1月1日前')
    expect(texts(accepted)).toContain('（已插入）')
    expect(collectRevisions(accepted).map((entry) => entry.revisionId)).toContain('rv-2')
  })

  it('清除批注保留正文，可指定 id', () => {
    const cleared = removeCommentsInModel(model())
    expect(collectComments(cleared)).toHaveLength(0)
    expect(texts(cleared)).toContain('在验收后十日内')

    const targeted = removeCommentsInModel(model(), ['c-missing'])
    expect(collectComments(targeted)).toHaveLength(1)
  })
})
