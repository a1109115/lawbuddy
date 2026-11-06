/**
 * 文书起草首页推荐卡（对齐参考图 1 的 12 项）。
 * 只负责展示与文书类型绑定，不承载模板匹配规则与提示词回填：
 * 具体模板由 Core 的 docgen_list_templates / docgen_create_free_draft 选择，
 * 选中后仅设置 templateId/docType，用户可直接发送（升级方案 §7.2）。
 */
export type DocGenPromptCategory = 'free' | 'litigation' | 'application' | 'letter' | 'opinion'

export interface DocGenRecommendation {
  key: string
  title: string
  desc: string
  docType: string
  category: DocGenPromptCategory
}

export const DOCGEN_RECOMMENDATIONS: DocGenRecommendation[] = [
  { key: 'free', title: '自由起草', desc: '自定义文书灵活生成', docType: '', category: 'free' },
  { key: 'complaint', title: '民事起诉状', desc: '精准明确诉讼请求', docType: '民事起诉状', category: 'litigation' },
  { key: 'appeal', title: '民事上诉状', desc: '精准针对判决上诉', docType: '民事上诉状', category: 'litigation' },
  { key: 'counterclaim', title: '民事反诉状', desc: '快速构建反诉文书', docType: '民事反诉状', category: 'litigation' },
  { key: 'defense', title: '民事答辩状', desc: '高效构建抗辩策略', docType: '民事答辩状', category: 'litigation' },
  { key: 'retrial', title: '民事再审申请书', desc: '针对生效判决上诉', docType: '民事再审申请书', category: 'application' },
  { key: 'lawyer-letter', title: '律师函', desc: '正式警告催告履约', docType: '律师函', category: 'letter' },
  { key: 'legal-opinion', title: '法律意见书', desc: '专业分析决策依据', docType: '法律意见书', category: 'opinion' },
  { key: 'enforcement', title: '强制执行申请书', desc: '兑现胜诉强制履行', docType: '强制执行申请书', category: 'application' },
  { key: 'labor-arbitration', title: '劳动仲裁申请书', desc: '劳动维权仲裁先行', docType: '劳动仲裁申请书', category: 'application' },
  { key: 'preservation', title: '财产保全申请书', desc: '防止转移保障执行', docType: '财产保全申请书', category: 'application' },
  { key: 'commercial-arbitration', title: '商事仲裁申请书', desc: '商事争议约定仲裁', docType: '商事仲裁申请书', category: 'application' }
]

export const FREE_DRAFT = DOCGEN_RECOMMENDATIONS[0]
export const DEFAULT_RECOMMENDATION_KEY = 'complaint'

/** 推荐卡语义覆盖的模板别名：推荐卡已展示的文书类型不再在「全部模板」重复出现 */
const COVERED_ALIASES: Record<string, string[]> = {
  强制执行申请书: ['申请执行书'],
  民事再审申请书: ['再审申请书'],
  劳动仲裁申请书: ['仲裁申请·劳动'],
  商事仲裁申请书: ['仲裁申请·商事'],
  财产保全申请书: ['财产保全']
}

const RECOMMENDED_TITLES = new Set(DOCGEN_RECOMMENDATIONS.map((item) => item.title))

/** 该模板是否与上方推荐卡重复（只隐藏完全同名或已归并的别名，细分变体保留在全部模板） */
export function recommendationCovers(template: { title: string; docType: string }): boolean {
  const title = template.title.trim()
  if (RECOMMENDED_TITLES.has(title)) return true
  return Object.entries(COVERED_ALIASES).some(([recommended, aliases]) =>
    RECOMMENDED_TITLES.has(recommended) &&
    aliases.some((alias) => title === alias || title.startsWith(`${alias}·`))
  )
}

/** 同名模板去重：优先保留带来源的正式模板；自由起草生成的临时模板没有来源 */
export function dedupeTemplates<T extends { title: string; sourceName?: string | null }>(templates: T[]): T[] {
  const byTitle = new Map<string, T>()
  for (const template of templates) {
    const key = template.title.trim()
    const current = byTitle.get(key)
    if (!current || (!current.sourceName && template.sourceName)) byTitle.set(key, template)
  }
  return [...byTitle.values()]
}

/** 推荐卡对应的精确同名模板；无唯一匹配时返回 null，由 Core 按文书类型自由起草 */
export function matchRecommendedTemplate<T extends { title: string; docType: string; sourceName?: string | null }>(
  item: DocGenRecommendation,
  templates: T[]
): T | null {
  if (!item.docType) return null
  const exact = templates.filter(
    (template) => template.title.trim() === item.docType && template.docType === item.docType
  )
  if (exact.length === 1) return exact[0]
  if (exact.length > 1) return exact.find((template) => template.sourceName) ?? null
  return null
}
