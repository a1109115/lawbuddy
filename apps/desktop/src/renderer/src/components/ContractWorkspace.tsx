import { Fragment, useEffect, useRef, useState, type JSX } from 'react'
import type { AgentTaskInfo, ContractChecklistItem, ContractChecklistResult, ContractChecklistSnapshot, ContractOverview, ContractOverviewConfig, ContractRiskRun } from '@lawbuddy/contracts'
import { CiteBadge } from './CiteBadge'
import { ChatView } from './ChatView'
import { Dock } from './Dock'
import { Icon } from './Icon'
import { WordWorkCopyEditor } from './preview/WordWorkCopyEditor'
import { errorMessage, getBridge } from '../services/bridge'
import { useApp, type DocMeta } from '../state/store'

const PARSE_LABEL: Record<ContractOverview['parseStatus'], string> = {
  no_document: '请上传主合同', missing: '主合同已移除', stale: '原文版本已变化，请重新保存概览',
  limited_scope: '存在缺页或低置信度文本', ready: '原文可核验', parsing: '等待解析或 OCR 完成'
}

const PARSE_TONE: Record<ContractOverview['parseStatus'], string> = {
  no_document: '', missing: '', stale: 'warn', limited_scope: 'warn', ready: 'ok', parsing: 'info'
}

const STAGES = [
  { key: 'overview', no: 1, label: '审查概览' },
  { key: 'checklist', no: 2, label: '审查清单' },
  { key: 'results', no: 3, label: '审查结果' }
] as const

const INTENSITIES: Array<{ value: ContractOverviewConfig['reviewIntensity']; title: string; desc: string }> = [
  { value: 'basic', title: '基础', desc: '关键条款检查' },
  { value: 'standard', title: '标准', desc: '交叉引用与权利义务' },
  { value: 'deep', title: '深度', desc: '跨条款与履约场景' }
]

const STANCE_PRESETS = ['甲方', '乙方', '中立审查']

/** 可作为主合同：解析/OCR 已完成（部分可读也可审，但不得仍有待识别页） */
function isPrimarySelectable(doc: DocMeta): boolean {
  return doc.ocrPending === 0 && (doc.status === 'ready' || doc.status === 'partial')
}

/** 未完成解析时的状态文案，用于下拉项后缀与选中态提示 */
function parseStateText(doc: DocMeta): string {
  if (doc.ocrPending > 0) return `解析中 ${doc.pages - doc.ocrPending}/${doc.pages} 页`
  switch (doc.status) {
    case 'need_password':
      return '待解锁'
    case 'failed':
      return '解析失败'
    case 'stale':
      return '已失效'
    case 'locked':
      return '已锁定'
    default:
      return '解析中'
  }
}

/** 选中了未解析完成的文档时的阻断说明 */
function parseBlockedMessage(doc: DocMeta): string {
  if (doc.ocrPending > 0) {
    return `仍在文字识别中（已完成 ${doc.pages - doc.ocrPending}/${doc.pages} 页），完成后才能保存概览并开始审查。`
  }
  switch (doc.status) {
    case 'failed':
      return '解析失败，请删除后重新导入或稍后重试。'
    case 'need_password':
      return '文件已加密，请先解锁后再作为主合同。'
    case 'stale':
      return '原文来源已变化，请重新导入后再作为主合同。'
    case 'locked':
      return '文档已锁定，无法作为主合同。'
    default:
      return '仍在解析中，完成后才能保存概览并开始审查。'
  }
}

const RESULT_TABS: Array<{ key: 'risk' | 'dialogue'; label: string; icon: string }> = [
  { key: 'risk', label: '风险审查', icon: 'i-shield' },
  { key: 'dialogue', label: '对话审查', icon: 'i-bulb' }
]

const FILTERS: Array<{ value: string; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'high', label: '高风险' },
  { value: 'medium', label: '中风险' },
  { value: 'low', label: '低风险' },
  { value: 'pass', label: '通过' },
  { value: 'needs_human', label: '待核实' }
]

const LEVEL_LABEL: Record<string, string> = {
  high: '高风险', medium: '中风险', low: '低风险', pass: '通过', needs_human: '待核实', none: '不适用'
}

/** 取清单项内最高风险等级；无风险项按状态给出圆点语义 */
const itemLevel = (item: ContractChecklistResult): string => {
  const order: Record<string, number> = { high: 3, medium: 2, low: 1 }
  const best = item.risks.reduce<string | null>((current, risk) =>
    (!current || (order[risk.riskLevel] ?? 0) > (order[current] ?? 0)) ? risk.riskLevel : current, null)
  return best ?? (item.status === 'pass' ? 'pass' : item.status === 'needs_human' ? 'needs_human' : 'none')
}

/**
 * 当前筛选下应展示的风险点。
 * 等级筛选只保留该等级的风险点——否则一个清单项同时含高/中风险时，
 * 选中「中风险」仍会把高风险内容一并列出来。
 */
const visibleRisksOf = (item: ContractChecklistResult, value: string): ContractChecklistResult['risks'] => {
  if (value === 'high' || value === 'medium' || value === 'low') {
    return item.risks.filter((risk) => risk.riskLevel === value)
  }
  if (value === 'needs_human') {
    return item.risks.filter((risk) => risk.cite.claimStatus !== 'supported')
  }
  return item.risks
}

const matchesFilter = (item: ContractChecklistResult, value: string): boolean => {
  if (value === 'all') return true
  if (value === 'pass' || value === 'not_applicable') return item.status === value
  if (value === 'needs_human') return item.status === 'needs_human' || visibleRisksOf(item, value).length > 0
  return visibleRisksOf(item, value).length > 0
}

/**
 * 长文本折叠：默认只显示前若干字，避免审查结论把卡片撑成整屏。
 * 未超长时不渲染展开按钮，不改变原始措辞。
 */
function FoldText({ text, lines = 2, className = '' }: { text: string; lines?: number; className?: string }): JSX.Element {
  const [open, setOpen] = useState(false)
  const limit = lines * 34
  const long = text.length > limit
  return (
    <div className={`cw-fold${open ? ' open' : ''}${className ? ` ${className}` : ''}`}>
      <p>{long && !open ? `${text.slice(0, limit)}…` : text}</p>
      {long && (
        <button type="button" className="cw-fold-toggle" onClick={() => setOpen((v) => !v)}>
          {open ? '收起' : '展开全文'}
        </button>
      )}
    </div>
  )
}

/** 判断标准输入：高度随内容增长（CSS field-sizing），长条款不再被固定行数裁在半行处 */
function CriterionInput({
  value,
  label,
  onChange
}: {
  value: string
  label: string
  onChange: (value: string) => void
}): JSX.Element {
  return (
    <textarea className="cw-criterion-input" aria-label={label} placeholder="判断标准"
      rows={3} value={value} onChange={(e) => onChange(e.target.value)} />
  )
}

/**
 * 运行中作业状态卡：把原始的 stage 代号翻成可读文案，并给出耗时与进度。
 * 长合同清单生成只有一个模型调用，没有中间事件，必须靠这里让用户看见“确实在跑”。
 */
function RunningCard({ job, elapsed, onCancel }: { job: AgentTaskInfo; elapsed: number; onCancel: () => void }): JSX.Element {
  const stage = job.stage ?? ''
  const risk = stage.match(/^contract_risk\s+(\d+)\/(\d+)$/)
  const waiting = job.lifecycle === 'WAITING_APPROVAL'
  const label = waiting ? '等待外发授权'
    : risk ? `正在逐项审查（${risk[1]} / ${risk[2]} 项）`
      : stage === 'contract_checklist' ? '正在生成审查清单草案'
        : '审查作业执行中'
  const detail = waiting ? '请在上方授权卡中允许或拒绝本次外发。'
    : risk ? '逐项比对原文并校验引用；已完成项保留，停止后可恢复。'
      : stage === 'contract_checklist' ? '正在读取合同原文并生成检查问题与判断标准；长合同通常需要数十秒。'
        : '正在执行，结果会自动刷新，无需刷新页面。'
  const total = risk ? Number(risk[2]) : 0
  const done = risk ? Number(risk[1]) : 0
  const pct = total > 0 ? Math.max(4, Math.round((done / total) * 100)) : null
  const mmss = `${String(Math.floor(elapsed / 60)).padStart(2, '0')}:${String(elapsed % 60).padStart(2, '0')}`
  return (
    <div className="cw-running" role="status" aria-live="polite">
      <span className="cw-running-orb" aria-hidden="true"><Icon id="i-bulb" /></span>
      <div className="cw-running-body">
        <div className="cw-running-head">
          <b>{label}</b>
          <span className="cw-running-time" title="本作业已用时">已用 {mmss}</span>
          <button className="btn-ghost cw-small" onClick={onCancel} disabled={waiting}>停止</button>
        </div>
        <p>{detail}</p>
        <div className={`cw-running-track${pct == null ? ' indet' : ''}`} role="progressbar"
          aria-valuenow={pct ?? undefined} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
          <i style={pct == null ? undefined : { width: `${pct}%` }} />
        </div>
      </div>
    </div>
  )
}

/** 清单生成期间的骨架占位：让空白区也从视觉上表现为“正在填充” */
function ChecklistSkeleton(): JSX.Element {
  return (
    <div className="cw-skeleton" aria-hidden="true">
      {[0, 1, 2].map((row) => (
        <div key={row} className="cw-skeleton-row" style={{ animationDelay: `${row * 0.18}s` }}>
          <span className="cw-skeleton-no" />
          <div className="cw-skeleton-copy">
            <span className="cw-skeleton-line" style={{ width: `${72 - row * 12}%` }} />
            <span className="cw-skeleton-line thin" style={{ width: `${90 - row * 10}%` }} />
          </div>
        </div>
      ))}
      <p className="cw-skeleton-tip"><span className="cw-spin" aria-hidden="true" />正在生成清单草案，完成后自动填入</p>
    </div>
  )
}

/** Contract state comes from Core snapshots, never inferred from tool messages. */
export function ContractWorkspace(): JSX.Element {
  const { currentCaseId, documents, importing, importFiles, openDoc, toast } = useApp()
  const [overview, setOverview] = useState<ContractOverview | null>(null)
  const [config, setConfig] = useState<Omit<ContractOverviewConfig, 'primaryVersionId'>>({ reviewIntensity: 'standard' })
  const [checklist, setChecklist] = useState<ContractChecklistSnapshot | null>(null)
  const [items, setItems] = useState<ContractChecklistItem[]>([])
  const [stage, setStage] = useState<'overview' | 'checklist' | 'results'>('overview')
  const [riskRun, setRiskRun] = useState<ContractRiskRun | null>(null)
  const [filter, setFilter] = useState('all')
  const [resultTab, setResultTab] = useState<'risk' | 'dialogue'>('risk')
  const [patchTarget, setPatchTarget] = useState('')
  const [editingCopy, setEditingCopy] = useState(false)
  const [replacements, setReplacements] = useState<Record<string, string>>({})
  const [applyConfirm, setApplyConfirm] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [job, setJob] = useState<AgentTaskInfo | null>(null)
  const [approval, setApproval] = useState<{ id: string; summary: string } | null>(null)
  // 审批已处理、任务却仍停在 WAITING_APPROVAL：需要用户恢复作业
  const [staleApproval, setStaleApproval] = useState(false)
  // 最近一次定位的风险：用于结果卡片选中态；原文预览统一走右侧文档视图
  const [activeRisk, setActiveRisk] = useState<{ id: string; pageNo: number; quote: string } | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // 已处理过的审批 ID：轮询事件不会清除历史 approval_request，必须本地记住，避免重复弹出
  const decidedApprovals = useRef<Set<string>>(new Set())
  // 审批处理时间：给执行线程一点时间恢复，避免刚点完就误报“作业卡住”
  const approvalDecidedAt = useRef(0)
  const activeJob = job && !['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED'].includes(job.lifecycle)
  // 作业用时：轮询只返回 stage，没有耗时字段；这里按任务 ID 从挂载起本地计时
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!activeJob) {
      setElapsed(0)
      return
    }
    const startedAt = Date.now()
    setElapsed(0)
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [job?.id, !!activeJob])
  const writable = useApp((s) => s.cases.some((c) => c.id === s.currentCaseId && c.status === 'active'))
  const primaryDocId = config.primaryDocumentId
  const primaryDoc = documents.find((doc) => doc.id === primaryDocId)
  // 主合同必须等解析/OCR 完成：未完成时下拉项置灰不可选，且不允许保存概览
  const primaryDocParsed = primaryDoc ? isPrimarySelectable(primaryDoc) : false

  useEffect(() => {
    setActiveRisk(null)
  }, [primaryDocId])

  // OCR/解析完成时刷新页文本：右侧阅读视图缓存的页不会自动更新
  const parseKey = primaryDoc ? `${primaryDoc.status}:${primaryDoc.ocrPending}` : ''
  const lastParseKey = useRef('')
  const invalidatePages = useApp((s) => s.invalidatePages)
  useEffect(() => {
    if (!primaryDocId || !primaryDoc) return
    const previous = lastParseKey.current
    lastParseKey.current = parseKey
    if (!previous || previous === parseKey) return
    if (primaryDoc.ocrPending === 0 && ['ready', 'partial', 'failed'].includes(primaryDoc.status)) {
      invalidatePages([primaryDocId])
    }
  }, [parseKey, primaryDocId, primaryDoc, invalidatePages])

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge || !currentCaseId || !writable) return
    let alive = true
    void bridge.contract.createOverview(currentCaseId).then(async (value) => {
      const saved = await bridge.contract.getChecklist({ overviewId: value.id })
      const reviewRun = await bridge.contract.getRiskRun(value.id)
      if (!alive) return
      setOverview(value)
      useApp.setState({ contractOverviewId: value.id })
      const { primaryVersionId: _version, ...savedConfig } = value.config
      setConfig({ reviewIntensity: 'standard', ...savedConfig })
      setChecklist(saved)
      setItems(saved?.items ?? [])
      setRiskRun(reviewRun)
      if (reviewRun) setJob(reviewRun.task)
      if (!reviewRun && value.latestChecklistTaskId) {
        const task = await bridge.agent.getTask(value.latestChecklistTaskId)
        if (!alive) return
        setJob(task)
      }
      setStage(reviewRun ? 'results' : saved && saved.status !== 'stale' ? 'checklist' : 'overview')
      // 已有工作副本时默认选中最新一份，应用建议无需再手动挑选；没有副本则应用时按需创建
      const copies = await bridge.word.listWorkCopies(currentCaseId).catch(() => [])
      if (alive) {
        const latest = copies.filter((copy) => copy.documentId === value.config.primaryDocumentId)
          .sort((a, b) => b.revision - a.revision)[0]
        if (latest) setPatchTarget((current) => current || latest.id)
      }
    }).catch((e) => { if (alive) setError(errorMessage(e)) })
    return () => { alive = false }
  }, [currentCaseId, writable])

  useEffect(() => {
    const bridge = getBridge()
    if (!bridge || !job || !overview || !activeJob) return
    let alive = true
    let inFlight = false
    const poll = async (): Promise<void> => {
      if (inFlight) return
      inFlight = true
      try {
        const [task, events] = await Promise.all([
          bridge.agent.getTask(job.id), bridge.agent.getEvents({ taskId: job.id })
        ])
        if (!alive) return
        const latestRisk = await bridge.contract.getRiskRun(overview.id)
        if (!alive) return
        setRiskRun(latestRisk)
        const hasApprovalEvent = events.some((event) => event.type === 'approval_request')
        // 审批结论事件由 Core 落库：窗口重开后据此过滤已有结论的历史审批
        const decidedFromCore = new Set(
          events.filter((event) => event.type === 'approval_decision')
            .map((event) => String(event.payload.approvalId))
        )
        const request = events.findLast((event) => event.type === 'approval_request'
          && !decidedApprovals.current.has(String(event.payload.approvalId))
          && !decidedFromCore.has(String(event.payload.approvalId)))
        if (task.lifecycle === 'WAITING_APPROVAL' && request) {
          setApproval({ id: String(request.payload.approvalId), summary: String(request.payload.dataSummary) })
          setStaleApproval(false)
        } else {
          setApproval(null)
          // 审批事件仍在历史，但已没有待处理审批；任务却还在等待 → 需要恢复（留 3 秒给执行线程）
          setStaleApproval(task.lifecycle === 'WAITING_APPROVAL' && hasApprovalEvent
            && Date.now() - approvalDecidedAt.current > 3000)
        }
        if (task.lifecycle === 'COMPLETED') {
          const saved = await bridge.contract.getChecklist({ overviewId: overview.id })
          if (alive) { setChecklist(saved); setItems(saved?.items ?? []); setDirty(false) }
        }
        if (task.lifecycle === 'FAILED') {
          const failure = events.findLast((event) => event.type === 'error')
          setError(String(failure?.payload.message ?? '清单生成失败，手工清单保留'))
        }
        if (alive) setJob(task)
      } catch (e) {
        if (alive) setError(errorMessage(e))
      } finally { inFlight = false }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1200)
    return () => { alive = false; window.clearInterval(timer) }
  }, [job?.id, overview?.id, !!activeJob])

  const run = async (action: () => Promise<void>): Promise<void> => {
    if (busy || !writable) return
    setBusy(true)
    setError('')
    try { await action() } catch (e) { setError(errorMessage(e)) } finally { setBusy(false) }
  }

  const saveOverview = (): void => {
    const bridge = getBridge()
    if (!bridge || !overview) return
    if (!primaryDocParsed) {
      toast(primaryDoc ? parseBlockedMessage(primaryDoc) : '主合同尚未解析完成，请稍后再保存概览')
      return
    }
    void run(async () => {
      const saved = await bridge.contract.updateOverview({
        overviewId: overview.id, config, expectedRevision: overview.revision
      })
      setOverview(saved)
      setChecklist(await bridge.contract.getChecklist({ overviewId: saved.id }))
      setStage('checklist')
      toast('概览已保存；请编辑并确认审查清单')
    })
  }

  const saveChecklist = (): void => {
    const bridge = getBridge()
    if (!bridge || !overview) return
    void run(async () => {
      const saved = await bridge.contract.updateChecklist({
        overviewId: overview.id, items, expectedRevision: checklist?.revision ?? 0
      })
      setChecklist(saved)
      setItems(saved.items)
      setDirty(false)
    })
  }

  const confirm = (): void => {
    const bridge = getBridge()
    if (!bridge || !overview || !checklist || dirty) return
    void run(async () => {
      setChecklist(await bridge.contract.confirmChecklist({
        overviewId: overview.id, expectedRevision: checklist.revision
      }))
      toast('清单快照已确认并绑定原文版本')
    })
  }

  const generate = (): void => {
    const bridge = getBridge()
    if (!bridge || !overview || dirty) return
    void run(async () => {
      const state = useApp.getState()
      const task = await bridge.contract.generateChecklist({ overviewId: overview.id, expectedRevision: overview.revision,
        modelPolicy: { remote: true, modelId: state.model, reasoningEffort: state.thinking } })
      setJob(task)
      toast('清单生成已发起；如弹出外发授权，请先允许本次作业')
    })
  }

  const startRisk = (): void => {
    const bridge = getBridge()
    if (!bridge || !overview || !checklist || dirty) return
    void run(async () => {
      const state = useApp.getState()
      const task = await bridge.contract.startRiskReview({
        overviewId: overview.id, snapshotId: checklist.id, clientRequestId: crypto.randomUUID(),
        modelPolicy: { remote: true, modelId: state.model, reasoningEffort: state.thinking }
      })
      setJob(task)
      setStage('results')
    })
  }

  const respond = (decision: 'allow_task' | 'deny'): void => {
    const bridge = getBridge()
    if (!bridge || !approval) return
    const approvalId = approval.id
    void run(async () => {
      // 服务端对已处理审批会抛错（安全评测要求不可复用）；这里视为“已有结论”并继续收尾
      let alreadyDecided = false
      let alreadyApproved = false
      try {
        await bridge.approval.respond({ approvalId, decision })
      } catch (e) {
        const message = errorMessage(e)
        if (!message.includes('审批请求已处理') && !message.toLowerCase().includes('already')) throw e
        alreadyDecided = true
        alreadyApproved = message.includes('approved')
        if (decision === 'deny' && alreadyApproved) {
          decidedApprovals.current.add(approvalId)
          approvalDecidedAt.current = Date.now()
          setApproval(null)
          setStaleApproval(false)
          setError('')
          toast('该审批此前已允许；如需停止请点击「停止」')
          return
        }
      }
      decidedApprovals.current.add(approvalId)
      approvalDecidedAt.current = Date.now()
      setApproval(null)
      setStaleApproval(false)
      setError('')
      if (!job) {
        toast(alreadyDecided ? '该审批已有结论' : '已处理审批')
        return
      }
      if (decision === 'deny') {
        // 拒绝后作业无法继续：结束挂起任务，避免按钮一直处于禁用状态
        const latest = await bridge.agent.getTask(job.id)
        if (['CREATED', 'PLANNING', 'RUNNING', 'VERIFYING', 'WAITING_HUMAN', 'WAITING_APPROVAL', 'PAUSED'].includes(latest.lifecycle)) {
          setJob(await bridge.agent.cancel(job.id))
          toast('已拒绝外发，本次审查作业已停止')
        } else {
          setJob(latest)
          toast('已拒绝外发')
        }
        return
      }
      // 允许：执行线程可能已不在（应用重开或审批在别处已完成），确认任务状态并恢复
      if (!alreadyDecided) await new Promise((resolve) => window.setTimeout(resolve, 1200))
      const latest = await bridge.agent.getTask(job.id)
      setJob(latest)
      if (['WAITING_APPROVAL', 'PAUSED'].includes(latest.lifecycle)) {
        setStaleApproval(true)
        toast(alreadyDecided ? '审批此前已允许，正在恢复作业' : '审批已通过，正在恢复作业')
        setJob(await bridge.agent.resume(job.id))
        setStaleApproval(false)
      } else {
        toast(alreadyDecided ? '审批此前已允许，作业进行中' : '已允许本次作业')
      }
    })
  }

  const resumeJob = (): void => {
    const bridge = getBridge()
    if (!bridge || !job) return
    void run(async () => {
      setJob(await bridge.agent.resume(job.id))
      setStaleApproval(false)
      toast('已恢复审查作业')
    })
  }

  /** 需要工作副本时按需创建（原件始终只读；已创建则复用） */
  const ensureWorkCopy = async (): Promise<string | null> => {
    if (patchTarget) return patchTarget
    const bridge = getBridge()
    if (!bridge) return null
    if (!currentCaseId || !primaryDocId) {
      setError('当前合同未绑定主文档，无法创建工作副本')
      return null
    }
    const created = await bridge.word.createWorkCopy({ caseId: currentCaseId, documentId: primaryDocId })
    setPatchTarget(created.id)
    return created.id
  }

  /** 打开工作副本编辑（统一 WPS 式编辑工作台；只改副本，不改原件） */
  const openCopyEditor = (): void => {
    if (useApp.getState().wordEditingDirty) {
      setError('请先保存并退出当前 Word 编辑，再打开工作副本编辑')
      return
    }
    setError('')
    void run(async () => {
      const id = await ensureWorkCopy()
      if (id) setEditingCopy(true)
    })
  }

  /**
   * 应用修改建议：不提供工作副本管理界面，副本按需生成。
   * 没有可写副本时先基于主合同创建一份，再写入；原件始终只读。
   */
  const applySuggestion = (riskId: string, originalReplacement: string): void => {
    const bridge = getBridge()
    if (!bridge || !overview || useApp.getState().wordEditingDirty) {
      setError('请先保存并退出 Word 编辑，再应用修改建议')
      return
    }
    void run(async () => {
      const targetId = await ensureWorkCopy()
      if (!targetId) return
      const target = await bridge.word.getModel(targetId)
      const result = await bridge.contract.applyRiskSuggestion({
        overviewId: overview.id, riskId, targetId, expectedRevision: target.revision,
        replacement: replacements[riskId] ?? originalReplacement, clientRequestId: crypto.randomUUID()
      })
      setApplyConfirm(null)
      setRiskRun(await bridge.contract.getRiskRun(overview.id))
      toast(`建议已作为修订写入工作副本 v${result.workCopy.revision}（含风险批注），原件不变`)
    })
  }

  const patchItem = (id: string, patch: Partial<ContractChecklistItem>): void => {
    setItems((rows) => rows.map((row) => row.id === id ? { ...row, ...patch } : row))
    setDirty(true)
  }
  const addItem = (): void => {
    setItems((rows) => [...rows, { id: crypto.randomUUID(), title: '', criterion: '',
      category: 'custom', enabled: true, origin: 'user', relatedSources: [] }])
    setDirty(true)
  }
  const riskItems = riskRun?.items ?? []
  const filteredItems = riskItems.filter((item) => matchesFilter(item, filter))
  const enabledCount = items.filter((item) => item.enabled).length
  const primaryDocName = documents.find((doc) => doc.id === primaryDocId)?.name ?? '未选择'
  const docParseStatus = overview && overview.config.primaryDocumentId === primaryDocId ? overview.parseStatus : null
  const docParseTone = docParseStatus ? PARSE_TONE[docParseStatus] : ''
  const docParseIcon = docParseStatus === 'ready' ? 'i-check' : docParseStatus === 'parsing' ? 'i-clock' : 'i-alert'
  const parseTone = overview ? PARSE_TONE[overview.parseStatus] : ''
  const parseIcon = overview?.parseStatus === 'ready' ? 'i-check' : overview?.parseStatus === 'parsing' ? 'i-clock' : 'i-alert'
  const stepDone = {
    overview: !!overview?.config.primaryDocumentId,
    checklist: checklist?.status === 'confirmed',
    results: riskRun?.status === 'completed'
  }
  const canConfirm = !busy && !activeJob && !dirty && !!checklist && checklist.status === 'draft' && writable
  const canStart = !busy && !activeJob && !dirty && checklist?.status === 'confirmed' && writable
  // 对话审查：对话区自己滚动，输入坞固定在页面最下方（与阅卷模式一致）
  const dialogueOpen = stage === 'results' && resultTab === 'dialogue'

  /**
   * 定位风险原文：统一打开右侧文档视图并按页码 + 引用片段高亮。
   * 中间区不再重复实现原文对照面板，避免同一份原件出现两套预览与两套定位逻辑。
   */
  const locateQuote = (id: string, pageNo: number | null | undefined, quote: string | undefined): boolean => {
    if (!primaryDocId) {
      toast('当前未绑定主合同，无法定位原文')
      return false
    }
    if (pageNo == null || !quote?.trim()) {
      toast('该结论缺少可定位原文，请人工核实')
      return false
    }
    setActiveRisk({ id, pageNo, quote })
    useApp.getState().openCitationTarget({
      documentId: primaryDocId, pageNo, quote, documentVersionId: overview?.config.primaryVersionId
    })
    return true
  }

  const locateRisk = (itemId: string, riskId: string, pageNo: number | null | undefined, quote: string | undefined): void => {
    setExpanded((rows) => ({ ...rows, [itemId]: true }))
    locateQuote(riskId, pageNo, quote)
  }

  return (
    <section className="contract-workspace cw" aria-label="合同审查工作台">
      <div className={`cw-review-pane${dialogueOpen ? ' dialogue' : ''}`}>
        <header className="cw-head">
          <div className="cw-head-main">
            <div className="cw-title-line">
              <span className="cw-mark" aria-hidden="true"><Icon id="i-shield" /></span>
              <h2>合同审查</h2>
              <span className="cw-fixed"><Icon id="i-shield" />任务模式已固定</span>
            </div>
            <p className="cw-sub">原件只读 · 审查结论逐条回溯原文 · 关键事实以原文为准</p>
          </div>
          <div className="cw-head-meta">
            {docParseStatus ? (
              <span className={`cw-parse ${docParseTone}`}>
                <Icon id={docParseIcon} />{PARSE_LABEL[docParseStatus]}
              </span>
            ) : primaryDocId ? (
              <span className="cw-parse info"><Icon id="i-clock" />待保存概览</span>
            ) : overview ? (
              <span className={`cw-parse ${PARSE_TONE[overview.parseStatus]}`}>
                <Icon id={parseIcon} />{PARSE_LABEL[overview.parseStatus]}
              </span>
            ) : null}
          </div>
        </header>

        <nav className="cw-steps" aria-label="审查阶段">
          {STAGES.map((step, stepIndex) => {
            const on = stage === step.key
            const done = stepDone[step.key]
            const disabled = step.key === 'checklist' ? !overview?.config.primaryDocumentId
              : step.key === 'results' ? !riskRun && !activeJob : false
            return (
              <Fragment key={step.key}>
                {stepIndex > 0 && <span className="cw-step-line" aria-hidden="true" />}
                <button type="button" className={`cw-step${on ? ' on' : ''}${done ? ' done' : ''}`}
                  aria-current={on ? 'step' : undefined} disabled={disabled} onClick={() => setStage(step.key)}>
                  <span className="cw-step-no">{step.no}</span>
                  <span className="cw-step-copy"><b>{step.label}</b></span>
                  {done && !on && <Icon id="i-check" className="cw-step-ok" />}
                </button>
              </Fragment>
            )
          })}
        </nav>

        {/* 外发授权会阻塞所有操作，必须固定在滚动区外，避免用户滚到底部时看不到 */}
        {approval && (
          <div className="cw-banner alert cw-approval" role="alert">
            <Icon id="i-shield" />
            <div className="cw-banner-copy">
              <b>需要确认外发范围</b>
              <p>{approval.summary}。模型调用可能产生费用，原文将在本地脱敏后外发。</p>
            </div>
            <div className="cw-banner-actions">
              <button className="btn-primary cw-banner-btn" disabled={busy} onClick={() => respond('allow_task')}>允许本次作业</button>
              <button className="btn-ghost cw-banner-btn" disabled={busy} onClick={() => respond('deny')}>拒绝外发</button>
            </div>
          </div>
        )}

        {staleApproval && !approval && (
          <div className="cw-banner warn cw-approval" role="alert">
            <Icon id="i-alert" />
            <div className="cw-banner-copy">
              <b>审批已完成，作业仍停在等待状态</b>
              <p>服务端已记录审批结果，但执行线程没有继续。恢复后会重新进入清单生成流程。</p>
            </div>
            <button className="btn-primary cw-banner-btn" disabled={busy || !writable} onClick={resumeJob}>
              <Icon id="i-redo" />恢复作业
            </button>
          </div>
        )}

        <div className={`cw-review-body${dialogueOpen ? ' dialogue' : ''}`}>
      {!writable && <p className="cw-note" role="status"><Icon id="i-shield" />当前任务只读，不能修改审查设置。</p>}
      {error && <div className="cw-banner warn" role="alert"><Icon id="i-alert" /><p>{error}</p></div>}
      {job && ['FAILED', 'CANCELLED'].includes(job.lifecycle) && (
        <div className="cw-banner muted" role="status">
          <Icon id="i-alert" />
          <p>上次审查作业未完成。可从已完成项恢复，不必重新开始。</p>
          <button className="btn-ghost cw-banner-btn" disabled={busy || !writable}
            onClick={() => void getBridge()?.agent.resume(job.id).then(setJob).catch((e) => setError(String(e)))}>
            <Icon id="i-redo" />从已完成项恢复作业
          </button>
        </div>
      )}
      {activeJob && !approval && (
        <RunningCard job={job} elapsed={elapsed}
          onCancel={() => void getBridge()?.agent.cancel(job.id).then(setJob).catch((e) => setError(errorMessage(e)))} />
      )}

      {stage === 'overview' && (
        <div className="cw-overview">
          <div className="cw-col-main">
            <section className="cw-card">
              <header className="cw-card-head">
                <span className="cw-card-ic"><Icon id="i-doc" /></span>
                <div>
                  <h3>主合同</h3>
                  <p>上传原件后选择本次审查的主合同；原件只读保存，不会被覆盖。</p>
                </div>
                <button className="btn-ghost cw-upload" disabled={importing || busy || !writable}
                  onClick={() => void importFiles()}>
                  {importing ? '导入中…' : <><Icon id="i-plus" />上传合同 / 附件</>}
                </button>
              </header>
              <div className="cw-card-body">
                <div className="cw-field">
                  <label className="cw-label" htmlFor="cw-primary-doc">主合同</label>
                  <div className="cw-select-wrap">
                    <select id="cw-primary-doc" className="cw-select" value={config.primaryDocumentId ?? ''}
                      onChange={(e) => setConfig((c) => ({ ...c, primaryDocumentId: e.target.value }))}>
                      <option value="">请选择已导入的合同</option>
                      {documents.map((doc) => {
                        const selectable = isPrimarySelectable(doc)
                        return (
                          <option key={doc.id} value={doc.id} disabled={!selectable}>
                            {doc.name}{selectable ? '' : ` · ${parseStateText(doc)}，解析完成后可选`}
                          </option>
                        )
                      })}
                    </select>
                    <Icon id="i-caret" className="cw-select-caret" />
                  </div>
                  {config.primaryDocumentId && primaryDoc && !primaryDocParsed ? (
                    <p className="cw-hint" role="status">
                      <Icon id="i-clock" />主合同{parseBlockedMessage(primaryDoc)}
                    </p>
                  ) : config.primaryDocumentId ? (
                    <button className="cw-text-btn" onClick={() => openDoc(config.primaryDocumentId!)}>
                      <Icon id="i-arrow" />在文档预览中查看
                    </button>
                  ) : (
                    <p className="cw-hint">尚未选择主合同。支持 PDF、DOCX 等已导入材料，上传后可在此选择。</p>
                  )}
                </div>
              </div>
            </section>

            <section className="cw-card">
              <header className="cw-card-head">
                <span className="cw-card-ic"><Icon id="i-gear" /></span>
                <div>
                  <h3>审查设置</h3>
                  <p>立场与强度决定审查视角和覆盖范围，会影响清单与风险判断。</p>
                </div>
              </header>
              <div className="cw-card-body">
                <div className="cw-field">
                  <label className="cw-label" htmlFor="cw-stance">审查立场</label>
                  <input id="cw-stance" className="cw-input" value={config.stance ?? ''}
                    placeholder="例如：代表合同中乙方 / 中立审查"
                    onChange={(e) => setConfig((c) => ({ ...c, stance: e.target.value }))} />
                  <div className="cw-chips" role="group" aria-label="常用审查立场">
                    {STANCE_PRESETS.map((stance) => (
                      <button key={stance} type="button" className={`cw-chip${config.stance?.trim() === stance ? ' on' : ''}`}
                        aria-pressed={config.stance?.trim() === stance}
                        onClick={() => setConfig((c) => ({ ...c, stance }))}>{stance}</button>
                    ))}
                  </div>
                </div>

                <div className="cw-field">
                  <span className="cw-label">审查强度</span>
                  <div className="cw-options" role="group" aria-label="审查强度">
                    {INTENSITIES.map((option) => (
                      <button key={option.value} type="button"
                        className={`cw-option${config.reviewIntensity === option.value ? ' on' : ''}`}
                        aria-pressed={config.reviewIntensity === option.value}
                        onClick={() => setConfig((c) => ({ ...c, reviewIntensity: option.value }))}>
                        <b>{option.title}</b><span>{option.desc}</span>
                      </button>
                    ))}
                  </div>
                </div>

                <div className="cw-field">
                  <label className="cw-label" htmlFor="cw-goal">交易目的 / 关注点 <em>选填</em></label>
                  <textarea id="cw-goal" className="cw-textarea" value={config.transactionGoal ?? ''}
                    placeholder="例如：本次采购需在 30 日内完成交付，重点核对付款节点、验收标准与违约责任。"
                    onChange={(e) => setConfig((c) => ({ ...c, transactionGoal: e.target.value }))} />
                </div>
              </div>
            </section>
          </div>

          <aside className="cw-col-side">
            <section className="cw-card cw-card-tight">
              <header className="cw-card-head">
                <span className="cw-card-ic"><Icon id="i-doc" /></span>
                <div>
                  <h3>原文与材料状态</h3>
                  <p>审查范围以可核验原文为边界。</p>
                </div>
              </header>
              <div className="cw-card-body">
                <div className="cw-parse-block">
                  <span className={`cw-parse ${parseTone}`}>
                    <Icon id={parseIcon} />
                    {overview ? PARSE_LABEL[overview.parseStatus] : '正在读取合同状态…'}
                  </span>
                  <p>{overview
                    ? `${overview.pageCount} 个解析页面；原文版本变化后需重新保存概览。`
                    : '选择主合同后即可继续审查设置。'}</p>
                </div>
                <dl className="cw-kv">
                  <div><dt>主合同</dt><dd>{primaryDocName}</dd></div>
                  <div><dt>审查强度</dt><dd>{INTENSITIES.find((i) => i.value === config.reviewIntensity)?.title ?? config.reviewIntensity}</dd></div>
                  <div><dt>原件</dt><dd>只读保存</dd></div>
                </dl>
                <label className="cw-toggle-row">
                  <input type="checkbox" checked={config.limitedScopeAcknowledged ?? false}
                    onChange={(e) => setConfig((c) => ({ ...c, limitedScopeAcknowledged: e.target.checked }))} />
                  <span>
                    <b>材料不完整时仅审查可核验部分</b>
                    <small>缺失页面和低置信度内容会保留「待核实」提示，不静默生成结论。</small>
                  </span>
                </label>
                <div className="cw-safety">
                  <span><Icon id="i-shield" />原件只读</span>
                  <span><Icon id="i-eye-off" />外发前脱敏</span>
                  <span><Icon id="i-link" />结论可回溯原文</span>
                </div>
              </div>
            </section>

            <div className="cw-side-tip">
              <Icon id="i-alert" />
              <p>如原文缺失、版本变化或 OCR 置信度不足，结论会标为待核实，需人工确认。</p>
            </div>
          </aside>

          <div className="cw-actionbar">
            <p><Icon id="i-check" />保存后进入清单编写；概览会绑定当前原文版本。</p>
            <button className="btn-primary cw-primary"
              disabled={busy || !writable || !config.primaryDocumentId || !primaryDocParsed || !config.stance?.trim()}
              title={!config.primaryDocumentId
                ? '请先选择主合同'
                : !primaryDocParsed
                  ? (primaryDoc ? parseBlockedMessage(primaryDoc) : '主合同不存在或尚未解析完成')
                  : !config.stance?.trim()
                    ? '请填写审查立场'
                    : undefined}
              onClick={saveOverview}>
              保存概览并进入清单<Icon id="i-arrow" />
            </button>
          </div>
        </div>
      )}

      {stage === 'checklist' && (
        <div className="cw-panel">
          <header className="cw-panel-head">
            <div>
              <div className="cw-panel-title">
                <h3>审查清单</h3>
                {checklist && <span className="cw-version">v{checklist.revision}</span>}
                {checklist && (
                  <span className={`cw-badge ${checklist.status === 'confirmed' ? 'ok' : checklist.status === 'stale' ? 'warn' : 'info'}`}>
                    {checklist.status === 'confirmed' ? '已确认' : checklist.status === 'stale' ? '已失效' : '草稿'}
                  </span>
                )}
              </div>
              <p>填写具体检查问题和判断标准。确认后保存不可变快照，再次编辑将生成新版本。</p>
            </div>
            <div className="cw-panel-meta">
              <span>{items.length} 项</span>
              <span>启用 {enabledCount}</span>
            </div>
          </header>

          {checklist?.status === 'stale' && (
            <div className="cw-banner warn" role="alert">
              <Icon id="i-alert" /><p>概览或原文已变化，请重新核对清单并保存新版本。</p>
            </div>
          )}
          {checklist?.status === 'confirmed' && (
            <div className="cw-banner ok" role="status">
              <Icon id="i-check" /><p>已确认 · 原文版本已绑定</p>
            </div>
          )}

          <div className="cw-items">
            {items.map((item, index) => (
              <article className={`cw-item${item.enabled ? '' : ' off'}`} key={item.id}>
                <div className="cw-item-head">
                  <span className="cw-item-no">{index + 1}</span>
                  <label className="cw-switch">
                    <input type="checkbox" checked={item.enabled} aria-label={`启用第 ${index + 1} 项检查`}
                      onChange={(e) => patchItem(item.id, { enabled: e.target.checked })} />
                    <span className="cw-switch-track" aria-hidden="true" />
                    <span className="cw-switch-text">启用</span>
                  </label>
                  <span className="cw-item-origin">{item.origin === 'ai' ? 'AI 草案' : '手工添加'}</span>
                  <div className="cw-item-tools">
                    <button className="cw-iconbtn" aria-label={`上移第 ${index + 1} 项`} title="上移"
                      disabled={busy || !!activeJob || !writable || index === 0}
                      onClick={() => {
                        setItems((rows) => {
                          const reordered = [...rows]
                          ;[reordered[index], reordered[index - 1]] = [reordered[index - 1], reordered[index]]
                          return reordered
                        })
                        setDirty(true)
                      }}><Icon id="i-up" /></button>
                    <button className="cw-iconbtn" aria-label={`下移第 ${index + 1} 项`} title="下移"
                      disabled={busy || !!activeJob || !writable || index === items.length - 1}
                      onClick={() => {
                        setItems((rows) => {
                          const reordered = [...rows]
                          ;[reordered[index], reordered[index + 1]] = [reordered[index + 1], reordered[index]]
                          return reordered
                        })
                        setDirty(true)
                      }}><Icon id="i-down" /></button>
                    <span className="cw-tool-sep" aria-hidden="true" />
                    <button className="cw-iconbtn danger" aria-label={`删除第 ${index + 1} 项检查`} title="删除"
                      disabled={busy || !!activeJob}
                      onClick={() => {
                        setItems((rows) => rows.filter((row) => row.id !== item.id)); setDirty(true)
                      }}><Icon id="i-x" /></button>
                  </div>
                </div>
                <input className="cw-title-input" aria-label={`第 ${index + 1} 项检查问题`} placeholder="检查问题"
                  value={item.title} onChange={(e) => patchItem(item.id, { title: e.target.value })} />
                <CriterionInput value={item.criterion} label={`第 ${index + 1} 项判断标准`}
                  onChange={(criterion) => patchItem(item.id, { criterion })} />
              </article>
            ))}
          </div>
          {!items.length && (
            activeJob ? <ChecklistSkeleton /> : (
              <div className="cw-empty">
                <Icon id="i-list" />
                <b>尚无审查清单</b>
                <span>新增检查项，或让 AI 基于当前合同生成清单草案。</span>
              </div>
            )
          )}

          <div className="cw-actionbar">
            <div className="cw-actionbar-left">
              <button className="btn-ghost" disabled={busy || !!activeJob || dirty || !writable} onClick={generate}
                title={approval ? '等待外发授权：请先允许本次作业'
                  : activeJob ? '审查作业进行中'
                    : dirty ? '有未保存修改，请先保存清单'
                      : !writable ? '当前任务只读' : undefined}>
                <Icon id="i-bulb" />AI 生成新清单草案
              </button>
              <button className="btn-ghost" disabled={busy || !!activeJob || !writable} onClick={addItem}
                title={approval ? '等待外发授权：请先允许本次作业' : activeJob ? '审查作业进行中' : undefined}>
                <Icon id="i-plus" />新增检查项
              </button>
              {(approval || activeJob) && (
                <span className="cw-action-hint">
                  {approval
                    ? <><Icon id="i-alert" />等待外发授权：请先允许本次作业</>
                    : <><span className="cw-spin" aria-hidden="true" />审查作业进行中 · 已用 {String(Math.floor(elapsed / 60)).padStart(2, '0')}:{String(elapsed % 60).padStart(2, '0')}</>}
                </span>
              )}
            </div>
            {dirty && <span className="cw-dirty"><Icon id="i-alert" />有未保存修改</span>}
            <div className="cw-actionbar-right">
              <button className="btn-ghost" disabled={busy || !!activeJob || !items.length || !writable} onClick={saveChecklist}>
                保存清单版本
              </button>
              <button className={canConfirm ? 'btn-primary' : 'btn-ghost'}
                disabled={busy || !!activeJob || dirty || !checklist || checklist.status !== 'draft' || !writable}
                onClick={confirm}>确认清单快照</button>
              <button className={canStart ? 'btn-primary' : 'btn-ghost'}
                disabled={busy || !!activeJob || dirty || checklist?.status !== 'confirmed'}
                onClick={startRisk}>开始风险审查</button>
            </div>
          </div>
        </div>
      )}

      {stage === 'results' && (
        <div className="cw-panel">
          <div className="cw-results-head">
            <div className="cw-seg" role="tablist" aria-label="审查结果类型">
              {RESULT_TABS.map((tab) => (
                <button key={tab.key} role="tab" aria-selected={resultTab === tab.key}
                  className={resultTab === tab.key ? 'on' : ''} onClick={() => setResultTab(tab.key)}>
                  <Icon id={tab.icon} />{tab.label}
                </button>
              ))}
            </div>
            <div className="cw-results-meta">
              {riskRun && (
                <span className={`cw-badge ${riskRun.status === 'completed' ? 'ok' : riskRun.status === 'stale' ? 'warn' : 'info'}`}>
                  {{ created: '待开始', running: '审查中', partial: '部分完成', completed: '已完成', stale: '已失效' }[riskRun.status]}
                </span>
              )}
              <span>清单项 {riskRun?.items.length ?? 0}/{riskRun?.total ?? 0}</span>
              {riskRun?.limitedScope && <span className="cw-meta-warn">范围受限</span>}
              <button
                className="btn-ghost cw-small"
                disabled={busy || !writable || !primaryDocId}
                title={
                  !primaryDocId
                    ? '当前未绑定主合同，无法创建工作副本'
                    : '打开工作副本编辑（原件只读，保存只改副本）'
                }
                onClick={openCopyEditor}
              >
                <Icon id="i-edit" />
                编辑工作副本
              </button>
            </div>
          </div>

          {resultTab === 'risk' && (
            <>
              {riskRun?.status === 'stale' && (
                <div className="cw-banner warn" role="alert">
                  <Icon id="i-alert" /><p>原文或清单已变化，此结果已失效，请重新审查。</p>
                </div>
              )}
              {riskRun?.limitedScope && (
                <div className="cw-banner warn" role="alert">
                  <Icon id="i-alert" /><p>仅覆盖可核验材料，缺失页面及低置信度内容仍需人工核实。</p>
                </div>
              )}

              <div className="cw-filters" role="group" aria-label="风险筛选">
                {FILTERS.map((entry) => (
                  <button key={entry.value} className={`cw-filter lv-${entry.value}${filter === entry.value ? ' on' : ''}`}
                    aria-pressed={filter === entry.value} onClick={() => setFilter(entry.value)}>
                    <i aria-hidden="true" />
                    <span>{entry.label}</span>
                    <em>{riskItems.filter((item) => matchesFilter(item, entry.value)).length}</em>
                  </button>
                ))}
              </div>

              <div className="cw-results-list">
                {filteredItems.map((item, index) => {
                  // 展开内容只列当前筛选命中的风险点，避免选中「中风险」却看到高风险
                  const risks = visibleRisksOf(item, filter)
                  const level = filter === 'high' || filter === 'medium' || filter === 'low' ? filter
                    : filter === 'needs_human' ? 'needs_human'
                      : itemLevel(item)
                  const open = expanded[item.itemId] ?? false
                  const isActive = risks.some((risk) => risk.id === activeRisk?.id)
                  return (
                    <article key={item.itemId}
                      className={`cw-result-card lv-${level}${open ? ' open' : ''}${isActive ? ' active' : ''}`}>
                      <div className="cw-result-head">
                        <span className={`cw-dot lv-${level}`} aria-hidden="true" />
                        <button type="button" className="cw-result-toggle" aria-expanded={open}
                          title={open ? '收起详情' : '展开详情'}
                          onClick={() => setExpanded((rows) => ({ ...rows, [item.itemId]: !open }))}>
                          <h4>
                            <span className="cw-result-no">{index + 1}.</span>
                            <span className="cw-result-title">{item.title}</span>
                          </h4>
                        </button>
                        <span className="cw-result-count" title={`${LEVEL_LABEL[level]} · ${risks.length} 项风险`}>
                          {risks.length || <Icon id="i-chevr" />}
                        </span>
                      </div>

                      {open && (
                        <div className="cw-result-body">
                          <div className="cw-result-summary">
                            <b className={`cw-level lv-${level}`}>{LEVEL_LABEL[level]}</b>
                            <FoldText text={item.reason} lines={3} className="cw-result-reason" />
                          </div>

                          {risks.map((risk, riskIndex) => {
                            // 优先使用 Claim 已核验的来源页码（模型页码可能偏差），无来源时回退模型页码
                            const pageNo = risk.cite.pageNo ?? risk.pageNo
                            // 「合同原文」必须展示真实原文：近似定位时用后端回填的实际匹配片段，
                            // 模型摘录与原文不一致的情况由下方提示显式说明。
                            const quote = risk.locatorQuote || risk.originalText || risk.cite.quote
                            const approximated = !!risk.locatorQuote && risk.locatorQuote !== risk.originalText
                            const locatable = pageNo != null && !!quote
                            const levelText = LEVEL_LABEL[risk.riskLevel]
                            return (
                              <section key={risk.id} className={`cw-risk lv-${risk.riskLevel}${risk.id === activeRisk?.id ? ' active' : ''}`}>
                                <header className="cw-risk-head">
                                  <b className="cw-risk-no">风险点 {riskIndex + 1}
                                    <span className="cw-risk-info" title={`等级：${levelText}`}><Icon id="i-alert" /></span>
                                  </b>
                                  <span className={`cw-level sm lv-${risk.riskLevel}`}>{levelText}</span>
                                  {risk.clauseRef && <span className="cw-risk-meta">{risk.clauseRef}</span>}
                                  {pageNo != null && <span className="cw-risk-meta">第 {pageNo} 页</span>}
                                </header>

                                <FoldText text={risk.riskNote} lines={3} className="cw-risk-note" />

                                {quote && (
                                  <div className="cw-block">
                                    <div className="cw-block-head">
                                      <span>合同原文</span>
                                      <button type="button" className="cw-text-btn" disabled={!locatable}
                                        title={locatable ? '在右侧文档预览中定位并高亮' : '缺少可定位原文，请人工核实'}
                                        onClick={() => locateRisk(item.itemId, risk.id, pageNo, quote)}>
                                        <Icon id="i-search" />定位至原文
                                      </button>
                                    </div>
                                    <p className="cw-block-text">{quote}</p>
                                    {approximated && (
                                      <p className="cw-hint">
                                        模型摘录与原文不完全一致，以上为原件实际匹配片段，请人工核对。
                                      </p>
                                    )}
                                  </div>
                                )}

                                {risk.replacementText && (
                                  <div className="cw-block suggest">
                                    <div className="cw-block-head">
                                      <span>建议更正条款为</span>
                                    </div>
                                    <textarea className="cw-block-textarea" rows={3}
                                      aria-label={`风险点 ${riskIndex + 1} 建议条款`}
                                      value={replacements[risk.id] ?? risk.replacementText}
                                      onChange={(e) => setReplacements((v) => ({ ...v, [risk.id]: e.target.value }))} />
                                  </div>
                                )}

                                {!risk.replacementText && risk.suggestion && (
                                  <div className="cw-block">
                                    <div className="cw-block-head"><span>处理建议</span></div>
                                    <p className="cw-block-text">{risk.suggestion}</p>
                                  </div>
                                )}

                                {risk.impact && (
                                  <p className="cw-risk-impact"><b>影响</b>{risk.impact}</p>
                                )}

                                <footer className="cw-risk-foot">
                                  <CiteBadge cite={risk.cite} onCite={(docId, page) => useApp.getState().openCitation(docId, page)} />
                                  <div className="cw-risk-ops">
                                    <button className="btn-ghost cw-small" disabled={!quote || !overview?.config.primaryDocumentId}
                                      onClick={() => void useApp.getState().addAnnotation(overview!.config.primaryDocumentId!,
                                        pageNo ?? 1, risk.riskNote, quote)}>添加批注</button>
                                    {risk.replacementText && (
                                      <button className="btn-primary cw-small"
                                        disabled={busy || risk.cite.claimStatus !== 'supported' || riskRun?.status !== 'completed' || risk.applications?.some((entry) => entry.targetId === patchTarget)}
                                        title={patchTarget ? undefined : '将先为当前合同创建一份工作副本，原件不变'}
                                        onClick={() => setApplyConfirm(risk.id)}>写入修订</button>
                                    )}
                                  </div>
                                </footer>

                                {risk.applications?.map((application) => (
                                  <p key={application.targetId} className="cw-hint">
                                    已应用：工作副本 {application.targetId.slice(0, 8)} · v{application.revision}
                                  </p>
                                ))}
                                {applyConfirm === risk.id && (
                                  <div className="cw-apply-confirm">
                                    <p>以“删除原文 + 插入建议”的修订形式写入工作副本，风险说明写成批注；律师可在工作副本中逐条接受或拒绝，原件不变。</p>
                                    <div className="cw-risk-ops">
                                      <button className="btn-primary cw-small" onClick={() => applySuggestion(risk.id, risk.replacementText)}>
                                        确认应用
                                      </button>
                                      <button className="btn-ghost cw-small" onClick={() => setApplyConfirm(null)}>取消</button>
                                    </div>
                                  </div>
                                )}
                              </section>
                            )
                          })}
                        </div>
                      )}
                    </article>
                  )
                })}
                {!filteredItems.length && (
                  <div className="cw-empty">
                    <Icon id="i-list" />
                    <b>{riskRun ? '当前筛选下没有结果' : '尚未生成审查结果'}</b>
                    <span>{riskRun ? '切换筛选条件查看其他清单项。' : '完成清单确认后，按确认清单开始风险审查。'}</span>
                  </div>
                )}
              </div>

              {/* 统计放在清单之后，避免一进结果页先看到汇总卡片 */}
              <section className="cw-card cw-card-tight">
                <div className="cw-card-body cw-stat-grid">
                  <div className="cw-stat"><b>{riskItems.length}<small>/{riskRun?.total ?? 0}</small></b><span>已完成</span></div>
                  <div className="cw-stat warn"><b>{riskItems.filter((item) => item.status === 'risk').length}</b><span>发现风险</span></div>
                  <div className="cw-stat warn"><b>{riskItems.filter((item) => item.status === 'needs_human').length}</b><span>待核实</span></div>
                  <div className="cw-stat ok"><b>{riskItems.filter((item) => item.status === 'pass').length}</b><span>通过</span></div>
                </div>
              </section>
            </>
          )}

          {resultTab === 'dialogue' && (
            <div className="cw-dialogue">
              <div className="cw-banner info">
                <Icon id="i-bulb" />
                <div className="cw-banner-copy">
                  <b>结合合同原文追问</b>
                  <p>聊天不会自动修改已确认清单或工作副本；涉及结论时请回到原文核对。</p>
                </div>
              </div>
              <ChatView />
            </div>
          )}
        </div>
      )}
        </div>

        {/* 审计提示固定在滚动区外 */}
        <p className="cw-audit"><Icon id="i-shield" />关键事实请以原文为准。无可靠来源的信息不能作为已支持事实。</p>
        {/* 对话审查：输入坞固定在页面最下方，对话区单独滚动 */}
        {dialogueOpen && <Dock />}
      </div>

      {editingCopy && patchTarget && (
        <div className="cw-editor-overlay" role="dialog" aria-modal="true" aria-label="编辑合同工作副本">
          <div className="cw-editor-overlay-head">
            <span><Icon id="i-edit" />编辑工作副本 · 原件只读</span>
            <span className="cw-editor-overlay-hint">编辑完成后点工具栏「完成」保存并返回审查</span>
          </div>
          <WordWorkCopyEditor
            targetId={patchTarget}
            onClose={() => {
              setEditingCopy(false)
              // 工作副本修订已更新：重新取一次风险作业，保持界面与最新副本一致
              const b = getBridge()
              if (b && overview) {
                void b.contract.getRiskRun(overview.id).then(setRiskRun).catch(() => undefined)
              }
            }}
          />
        </div>
      )}
    </section>
  )
}
