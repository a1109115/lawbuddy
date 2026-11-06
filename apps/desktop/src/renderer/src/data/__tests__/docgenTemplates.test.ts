import { describe, expect, it } from 'vitest'
import {
  DEFAULT_RECOMMENDATION_KEY,
  DOCGEN_RECOMMENDATIONS,
  FREE_DRAFT,
  dedupeTemplates,
  matchRecommendedTemplate,
  recommendationCovers
} from '../docgenTemplates'

describe('文书起草推荐卡数据', () => {
  it('提供 12 项推荐且每项都有标题与一句话描述', () => {
    expect(DOCGEN_RECOMMENDATIONS).toHaveLength(12)
    for (const item of DOCGEN_RECOMMENDATIONS) {
      expect(item.key).toBeTruthy()
      expect(item.title).toBeTruthy()
      expect(item.desc).toBeTruthy()
      expect(item.category).toBeTruthy()
    }
  })

  it('默认选中民事起诉状', () => {
    const found = DOCGEN_RECOMMENDATIONS.find((item) => item.key === DEFAULT_RECOMMENDATION_KEY)
    expect(found?.title).toBe('民事起诉状')
    expect(found?.docType).toBe('民事起诉状')
  })

  it('推荐卡只携带文书类型，不再预置起草提示词', () => {
    const complaint = DOCGEN_RECOMMENDATIONS.find((item) => item.key === 'complaint')!
    expect(complaint.docType).toBe('民事起诉状')
    // 回填提示词已移除：数据里不得再有 prompt 字段，避免旧心智回流
    expect(Object.keys(complaint)).not.toContain('prompt')
  })

  it('自由起草不绑定文书类型', () => {
    expect(FREE_DRAFT.category).toBe('free')
    expect(FREE_DRAFT.docType).toBe('')
  })
})

describe('全部模板与推荐卡去重', () => {
  it('推荐卡已覆盖的同名与归并别名不再重复展示，细分变体保留', () => {
    expect(recommendationCovers({ title: '律师函', docType: '律师函' })).toBe(true)
    expect(recommendationCovers({ title: '仲裁申请·劳动', docType: '仲裁申请·劳动' })).toBe(true)
    expect(recommendationCovers({ title: '申请执行书', docType: '申请执行书' })).toBe(true)
    expect(recommendationCovers({ title: '民事起诉状·离婚', docType: '民事起诉状·离婚' })).toBe(false)
    expect(recommendationCovers({ title: '证据目录', docType: '证据目录' })).toBe(false)
    expect(recommendationCovers({ title: '执行异议·行为', docType: '执行异议·行为' })).toBe(false)
  })

  it('同名模板优先保留带来源的正式模板', () => {
    const deduped = dedupeTemplates([
      { title: '律师函', sourceName: null, id: 'free' },
      { title: '律师函', sourceName: '范本.md', id: 'curated' },
      { title: '证据目录', sourceName: null, id: 'dir' }
    ])
    expect(deduped).toHaveLength(2)
    expect(deduped[0].id).toBe('curated')
  })

  it('推荐卡只绑定精确同名模板，细分变体留给全部模板或自由起草', () => {
    const complaint = DOCGEN_RECOMMENDATIONS.find((item) => item.key === 'complaint')!
    const letter = DOCGEN_RECOMMENDATIONS.find((item) => item.key === 'lawyer-letter')!
    const templates = [
      { id: 'c1', title: '民事起诉状·离婚', docType: '民事起诉状·离婚', sourceName: '范本.md' },
      { id: 'l1', title: '律师函', docType: '律师函', sourceName: '范本.md' }
    ]
    expect(matchRecommendedTemplate(complaint, templates)).toBeNull()
    expect(matchRecommendedTemplate(letter, templates)?.id).toBe('l1')
  })
})
