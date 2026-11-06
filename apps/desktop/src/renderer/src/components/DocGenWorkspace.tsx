import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import type { DocGenDraft, DocGenTemplate } from '@lawbuddy/contracts'
import {
  DEFAULT_RECOMMENDATION_KEY,
  DOCGEN_RECOMMENDATIONS,
  dedupeTemplates,
  matchRecommendedTemplate,
  recommendationCovers,
  type DocGenRecommendation
} from '../data/docgenTemplates'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { Dock } from './Dock'
import { Icon } from './Icon'

const STATUS_LABEL: Record<string, string> = {
  outline: '提纲待确认',
  draft: '草稿',
  confirmed: '已定稿'
}

/** 文书起草工作台：推荐卡 + 页内输入卡 + 全部模板 + 最近文书（对话由 Center 路由） */
export function DocGenWorkspace(): JSX.Element {
  const { currentCaseId, input, setDocgenStartOpen, openDocGen } = useApp()
  const [templates, setTemplates] = useState<DocGenTemplate[]>([])
  const [drafts, setDrafts] = useState<DocGenDraft[] | null>(null)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [allOpen, setAllOpen] = useState(false)
  // 已绑定模板时不再默认高亮推荐卡，避免与「全部模板」的选中态同时出现
  const [selectedKey, setSelectedKey] = useState<string | null>(() =>
    useApp.getState().bestPracticeTemplateId ? null : DEFAULT_RECOMMENDATION_KEY
  )
  const defaultApplied = useRef(false)
  // 选中态以 store 中已绑定的模板为准，切换页面后仍能看出当前选中的模板
  const boundTemplateId = useApp((s) => s.bestPracticeTemplateId)

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge || !currentCaseId) return
    let alive = true
    void bridge.docgen
      .installBestPractices(currentCaseId)
      .then(async () => {
        const list = await bridge.docgen.listTemplates(currentCaseId)
        if (!alive) return
        setTemplates(list.filter((template) => template.status === 'active'))
        const recent = await bridge.docgen.listDrafts(currentCaseId).catch(() => [])
        if (alive) setDrafts(recent)
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [currentCaseId])

  // 起草首页按设计稿隐藏右侧文档面板，让推荐卡获得足够横向空间；
  // 离开首页（进入对话或工作台）时恢复，除非用户已手动打开。
  useEffect(() => {
    const s = useApp.getState()
    const hidByHome = !s.rightHide
    if (hidByHome) s.toggleRight()
    return () => {
      if (hidByHome && useApp.getState().rightHide) useApp.getState().toggleRight()
    }
  }, [])

  // 首次进入默认选中「民事起诉状」：只绑定文书类型，不回填提示词、不覆盖用户输入
  useEffect(() => {
    if (defaultApplied.current || input.trim()) return
    const item = DOCGEN_RECOMMENDATIONS.find((x) => x.key === DEFAULT_RECOMMENDATION_KEY)
    if (!item) return
    defaultApplied.current = true
    useApp.setState({ bestPracticeTemplateId: null, docgenDocType: item.docType })
  }, [input])

  const selectRecommendation = (item: DocGenRecommendation): void => {
    setSelectedKey(item.key)
    // 推荐卡与模板库有精确同名模板时直接绑定，避免模型再次检索模板失败
    const matched = matchRecommendedTemplate(item, templates)
    useApp.setState({
      bestPracticeTemplateId: matched?.id ?? null,
      docgenDocType: matched?.docType ?? (item.docType || null)
    })
    // 只选模板即可直接发送；不再把长段提示词灌进输入框（升级方案 §7.2）
    useApp.getState().focusInput()
  }

  const selectTemplate = (template: DocGenTemplate): void => {
    setSelectedKey(null)
    useApp.setState({
      bestPracticeTemplateId: template.id,
      docgenDocType: template.docType
    })
    useApp.getState().focusInput()
  }

  // 全部模板：同名版本去重，且不再重复展示上方推荐卡已覆盖的文书类型
  const available = useMemo(
    () => dedupeTemplates(templates).filter((template) => !recommendationCovers(template)),
    [templates]
  )
  const filtered = useMemo(
    () => available.filter((template) => `${template.title} ${template.docType}`.includes(search.trim())),
    [available, search]
  )

  return (
    <section className="contract-workspace docgen-home" aria-label="文书起草工作台">
      <header className="docgen-home-head">
        <h2>文书起草，专业法律文书生成</h2>
      </header>

      <Dock variant="inline" />

      <div className="docgen-rec-head">
        <span>推荐</span>
      </div>
      <div className="docgen-rec-grid">
        {DOCGEN_RECOMMENDATIONS.map((item) => (
          <button
            key={item.key}
            className={`docgen-rec-card${selectedKey === item.key ? ' sel' : ''}`}
            aria-pressed={selectedKey === item.key}
            onClick={() => selectRecommendation(item)}
          >
            <b>{item.title}</b>
            <span>{item.desc}</span>
          </button>
        ))}
      </div>

      <div className="docgen-home-ops">
        <button className="btn-ghost" onClick={() => setAllOpen(!allOpen)}>
          <Icon id={allOpen ? 'i-minus' : 'i-list'} />
          {allOpen ? '收起全部模板' : `全部模板（${available.length}）`}
        </button>
        <button className="btn-ghost" onClick={() => setDocgenStartOpen(true)}>
          <Icon id="i-plus" />
          模板管理与导入
        </button>
      </div>

      {allOpen && (
        <div className="docgen-all" role="region" aria-label="全部文书模板">
          <input
            className="form-input"
            aria-label="搜索文书模板"
            placeholder={`搜索 ${available.length} 项模板`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="docgen-template-grid">
            {filtered.map((template) => {
              const isSel = boundTemplateId === template.id
              return (
                <button
                  key={template.id}
                  className={`docgen-template-card${isSel ? ' sel' : ''}`}
                  aria-pressed={isSel}
                  onClick={() => selectTemplate(template)}
                >
                  <b>{template.title}</b>
                  <span>
                    文书类型：{template.docType || '未设置'}
                    {template.sourceName ? ` · 来源：${template.sourceName}` : ' · 内置范本'}
                    {template.sourceFormat === 'docx'
                      ? ` · Word 模板（${template.templateMode === 'strict' ? '变量替换' : '参考范本'}）`
                      : ''}
                    {` · v${template.versionNo}`}
                  </span>
                </button>
              )
            })}
          </div>
          {filtered.length === 0 && <p className="docgen-empty-line">没有匹配的模板，可导入本地范本。</p>}
        </div>
      )}

      {drafts && drafts.length > 0 && (
        <div className="docgen-recent" aria-label="最近文书">
          <div className="docgen-recent-head">
            <Icon id="i-clock" />
            最近文书
          </div>
          <div className="docgen-recent-list">
            {drafts.slice(0, 5).map((draft) => (
              <button key={draft.id} className="docgen-recent-item" onClick={() => openDocGen(draft.id)}>
                <span className="docgen-recent-doc">
                  <Icon id="i-doc" />
                </span>
                <span className="docgen-recent-copy">
                  <b>{draft.docType || draft.templateTitle || '未命名文书'}</b>
                  <span>
                    {STATUS_LABEL[draft.status] ?? draft.status}
                    {draft.blockingCount > 0 ? ` · ${draft.blockingCount} 处待补充` : ''}
                    {draft.updatedAt ? ` · ${draft.updatedAt.slice(0, 16).replace('T', ' ')}` : ''}
                  </span>
                </span>
                <Icon id="i-chevr" />
              </button>
            ))}
          </div>
        </div>
      )}

      {error && (
        <p className="contract-warning" role="alert">
          {error}
        </p>
      )}
      {!templates.length && !error && <p className="docgen-empty-line">正在加载本地参数化模板，失败时可导入本地范本。</p>}

      <p className="contract-audit">关键事实请以原文为准；未确认信息以 xx 标注，定稿前请核实替换。</p>
    </section>
  )
}
