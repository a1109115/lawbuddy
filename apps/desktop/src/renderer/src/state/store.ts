import { create } from 'zustand'
import type {
  AgentTaskEvent,
  AiMessageBody,
  AnnotationColor,
  ArtifactAnnotation,
  ArtifactExportFormat,
  ArtifactMeta,
  ApprovalCard,
  CaseInfo,
  CitationTarget,
  ChatAttachment,
  ChatHistoryTurn,
  ClaimSummary,
  ContractStartInput,
  ModelCatalog,
  ModelProviderInfo,
  ProviderModelInfo,
  SaveProviderRequest,
  DocGenStartInput,
  DocumentAnnotation,
  DocumentPage,
  EvidenceBundleResult,
  HumanAnswer,
  HumanQuestionSet,
  ImportOutcome,
  MemoryEntry,
  ReasoningEffort,
  RedactionPolicy,
  SkillInfo
} from '@lawbuddy/contracts'
import { MODES, YJ_PRESETS, badge, type ModeKey, type PresetKey } from '../data/mock'
import { errorMessage, getBridge } from '../services/bridge'
import { supportsOriginalPreview } from '../services/preview'

/** 产物批注映射为 DocumentAnnotation 兼容形状（无版本/页模型：pageNo 恒 1），
 * 让 PageFrame/AnnotationItem 等既有展示组件无需按产物分支 */
function artifactAnnToDocAnn(a: ArtifactAnnotation): DocumentAnnotation {
  return {
    id: a.id,
    documentId: a.artifactId,
    versionId: '',
    pageNo: 1,
    quote: a.quote,
    quoteMissing: a.quoteMissing,
    body: a.body,
    color: a.color,
    status: a.status,
    resolvedAt: a.resolvedAt,
    createdAt: a.createdAt,
    updatedAt: a.updatedAt
  }
}

/** 模型条目反查（目录未加载时返回 null：能力门禁按不支持处理，fail-closed） */
export function findCatalogModel(
  catalog: ModelCatalog | null,
  modelId: string
): ProviderModelInfo | null {
  for (const p of catalog?.providers ?? []) {
    const hit = p.models.find((m) => m.id === modelId)
    if (hit) return hit
  }
  return null
}

/** 模型归属服务商 id（同名模型取目录首个声明者）；未声明返回 null */
export function providerOfModel(catalog: ModelCatalog | null, modelId: string): string | null {
  for (const p of catalog?.providers ?? []) {
    if (p.models.some((m) => m.id === modelId)) return p.id
  }
  return null
}

/** 按模型目录收敛思考强度：档位不被该模型支持时回落到它的默认档（无档位则关闭） */
function normalizeThinkingForModel(
  catalog: ModelCatalog | null,
  modelId: string,
  prefer?: ReasoningEffort
): ReasoningEffort {
  const conf = findCatalogModel(catalog, modelId)
  // 目录未加载或模型未声明（交给网关按未设置处理）：保留原档位，不擅自改成「关闭」
  if (!conf) return prefer ?? 'off'
  const levels = (conf.reasoningLevels ?? []) as Exclude<ReasoningEffort, 'off'>[]
  if (!levels.length) return 'off'
  if (prefer && (prefer === 'off' || levels.includes(prefer))) return prefer
  return (conf.reasoningDefault || levels[levels.length - 1]) as ReasoningEffort
}

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** 任务执行步骤（事件流驱动） */
export interface TaskStep {
  label: string
  state: 'done' | 'active'
  detail?: string
}

/** 引用角标数据（answer_verification 事件产出；键 = `标题|页码`，无页码引用页码为 0） */
export interface CiteMarkInfo {
  title: string
  pageNo: number
  /** 页码区间引用的结束页（第N-M页；缺省无区间） */
  pageEnd?: number
  documentId?: string
  claimStatus?: string
  /** 页面片段预览（悬浮出处卡展示，≤120 字） */
  snippet?: string
}

/** 自主 Agent Loop 消息流条目（6 事件协议的渲染模型，UI 纯事件驱动追加） */
export interface TaskChatItem {
  kind: 'assistant' | 'tool' | 'toolResult'
  /** assistant：该轮完整文本 */
  text?: string
  round?: number
  /** tool / toolResult：调用标识与工具名 */
  callId?: string
  name?: string
  /** tool：参数 JSON 文本 */
  args?: string
  status?: 'running' | 'ok' | 'error' | 'stopped'
  /** toolResult：结果摘要（截断展示，避免消息流与持久化膨胀） */
  resultPreview?: string
  /** tool（render_html）：完整 HTML（事件通道原文，供气泡内预览卡渲染；持久化时剥离） */
  html?: string
  /** tool（render_html）：预览卡标题 */
  title?: string
  /** tool（render_html）：已注册的预览地址（lawbuddy-preview://<内容哈希>，持久化保留；历史会话据此恢复预览） */
  previewUrl?: string
  /** tool（case_explore）：子代理进度文案（已读取 N 个文件 · 检索 M 次） */
  progress?: string
}

export type Msg =
  | {
      id: string
      role: 'user'
      text: string
      createdAt?: number // createdAt：发送时刻（气泡底部展示）
      skillNames?: string[] // 本次任务显式选定的技能名（/ 菜单选中；气泡上展示，便于回看本轮绑定）
      attachmentNames?: string[] // 本次任务随问附件名（截图/图片直发模型；气泡上留痕）
    }
  | { id: string; role: 'ai'; typing: true } // 浏览器调试模式 fallback
  | { id: string; role: 'ai'; typing?: false; body: AiMessageBody }
  | {
      id: string
      role: 'task'
      taskId: string
      lifecycle: string
      steps: TaskStep[]
      chat?: TaskChatItem[] // 自主 Loop 消息流（assistant / tool / toolResult，追加式）
      startedAt?: number // 任务发起时刻（epoch ms）：执行中实时计时与总耗时兜底
      endedAt?: number // 任务到达终态时刻（epoch ms）：气泡底部「完成于」展示
      turn?: {
        rounds: number
        reason: string
        forcedFinal?: boolean // 轮数用尽后追发无工具调用强制作答（结论已产出）
        promptTokens?: number
        completionTokens?: number
        durationMs?: number // 任务总用时
        /** 完成守门结论（05 方案 §7）：ok=false 时有明确列出的待核实/未读/产物警告 */
        finalize?: {
          ok: boolean
          taskDepth: string
          warnings: Array<{ code: string; text: string; files?: string[] }>
          coverage: {
            required: boolean
            totalPages: number
            readPages: number
            unread: number
            excluded?: number
          }
        }
      } // turn_complete：轮数、结束原因与用时
      body?: AiMessageBody
      streamText?: string // 流式累积原始文本（message_delta 覆盖式 / text 追加式）
      streamPreview?: string // 可读成稿片段（message_delta.preview，优先渲染）
      streamIncomplete?: boolean // FR-002：流式中断，内容不完整
      thinkingChars?: number // 本轮深度思考已累计字符（thinking_progress；长思考期防「假卡死」）
      /** 引用角标数据（answer_verification；键 = `标题|页码`） */
      cites?: Record<string, CiteMarkInfo>
      lastSeq?: number // 已消费事件游标（推送与轮询按 seq 去重）
      error?: string
      approval?: { card: ApprovalCard; decided?: string }
      /** 文书生成的人机决策问题集（请求用户决策时挂载；回答后保留为只读摘要） */
      questionSet?: HumanQuestionSet
      /** 会话级任务清单（plan_update 覆盖式更新；Trae 式「N/M 已完成」卡片数据源） */
      taskPlan?: Array<{ text: string; status: string }>
    }

export interface ToastItem {
  id: number
  text: string
  leaving?: boolean
}

/** 历史对话会话（本地持久化，PRD 9.3 历史记录） */
export interface ChatSession {
  id: string
  title: string
  mode: ModeKey
  caseId: string | null
  updatedAt: number
  messages: Msg[]
  /** Core 侧会话锚点（V26）：同会话续聊时随 agent.start 续传，跨任务历史全文由 Core 持久化 */
  conversationId?: string
}

/** 真实导入的文档（渲染层视图模型） */
export interface DocMeta {
  id: string
  name: string
  type: string // docx/pdf/xlsx/txt/md/png...
  date: string
  pages: number
  status: string
  ocrPending: number
  /** 所属案件（与当前任务不同表示关联案件只读共享材料） */
  caseId?: string
  /** 解析文本已原地脱敏（文本视图黄底高亮打码位置，原件预览显示原始内容） */
  isRedacted?: boolean
}

/** 阅读视图页元数据（O-008 人工更正入口所需的最小集合） */
export interface OcrPageMeta {
  pageNo: number
  quality: DocumentPage['quality']
  corrected: boolean
  ocrConfidence?: number | null
}

/** 阅读视图统一文档：真实卷宗（页文本）或 AI 产物（结构化 body，M2 收尾） */
export interface RenderDoc {
  id: string
  name: string
  type: string
  /** 原件类型（pdf/docx/xlsx/md/html/txt/png/jpg/tiff…）：决定原件预览渲染器 */
  kind: string
  date: string
  pages: number
  /** 所属案件（与当前任务不同表示关联案件只读共享材料） */
  caseId?: string
  body?: AiMessageBody
  pageTexts?: string[]
  isArtifact?: boolean
  /** 文件类产物（kind=file，AI 生成的 Word/Excel/PPT 等）：kind 为真实扩展名，走原件预览 */
  isFileArtifact?: boolean
  /** 文书生成落盘的 Word（编辑统一走文书工作台，不在阅读视图直接改文件） */
  docgenDraftId?: string
  /** 派生脱敏版：文本视图将高亮打码位置并强制走文本渲染 */
  isRedacted?: boolean
}

export interface NewCaseForm {
  title: string
  caseType: CaseInfo['caseType']
  clientName: string
  clientRole: string
  stage: string
  defaultMode: ModeKey
  /** 文书生成任务关联案件（只读共享其材料与案件记忆） */
  linkedCaseId?: string
  clientRequestId?: string
  contractType?: string
  reviewBackground?: string
}

/** 'docgen' 仅用于兼容旧版本持久化布局；运行时已统一复用文档预览（read） */
type RightView = 'list' | 'read' | 'docgen'

/** 阅读视图呈现方式（REQ-DOC-014）：原件真实预览 / 提取文本 */
export type PreviewMode = 'render' | 'text'

interface LayoutSnap {
  leftW: number
  rightW: number
  leftHide: boolean
  rightHide: boolean
  rightView: RightView
}

const DEF_LEFT = 264
const DEF_RIGHT = 640
const LEFT_MIN = 180
const LEFT_MAX = 340
const RIGHT_MIN = 280
const CURRENT_CASE_KEY = 'lawbuddy.currentCase'
const HISTORY_KEY = 'lawbuddy.chatHistory'
const HISTORY_MAX = 20

/**
 * 会话历史的存储键随工作空间变化：不同工作空间的案件与对话互不可见，
 * 不能共用同一份历史。空值表示尚未确定工作空间（启动早期 / 浏览器调试）。
 */
let activeWorkspaceKey = ''
let historyMigrated = false
let storageMigrated = false

/** 改名兼容（Slvyue -> LawBuddy）：把旧前缀的本地键与值里的旧工作空间路径迁移一次。 */
function migrateLegacyStorage(): void {
  if (storageMigrated) return
  storageMigrated = true
  try {
    const moves: Array<[string, string]> = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith('slvyue.')) continue
      moves.push([key, `lawbuddy.${key.slice('slvyue.'.length)}`])
    }
    for (const [from, to] of moves) {
      const raw = localStorage.getItem(from)
      if (raw === null || localStorage.getItem(to) !== null) continue
      localStorage.setItem(to, raw.replaceAll('SlvyueWorkspace', 'LawBuddyWorkspace'))
      localStorage.removeItem(from)
    }
  } catch {
    /* 存储不可用时跳过迁移 */
  }
}

function historyKey(): string {
  return activeWorkspaceKey ? `${HISTORY_KEY}.${activeWorkspaceKey}` : HISTORY_KEY
}

/** 当前案件的记忆键同样按工作空间隔离：切空间后不能恢复另一空间的案件 id */
function currentCaseKey(): string {
  return activeWorkspaceKey ? `${CURRENT_CASE_KEY}.${activeWorkspaceKey}` : CURRENT_CASE_KEY
}

/** 工作空间根目录 → localStorage 键片段（去掉分隔符与冒号，避免键名歧义） */
function workspaceKeyOf(rootUri: string): string {
  return rootUri.trim().toLowerCase().replace(/[\\/:]+/g, '_').replace(/_+$/, '')
}

/** 切换到某个工作空间前调用：确定历史键，并把旧版本全局历史迁移到该工作空间一次 */
function useWorkspaceHistory(rootUri: string): void {
  migrateLegacyStorage()
  activeWorkspaceKey = workspaceKeyOf(rootUri)
  if (historyMigrated) return
  historyMigrated = true
  try {
    const legacy = localStorage.getItem(HISTORY_KEY)
    if (legacy && !localStorage.getItem(historyKey())) {
      localStorage.setItem(historyKey(), legacy)
      localStorage.removeItem(HISTORY_KEY)
    }
    const legacyCase = localStorage.getItem(CURRENT_CASE_KEY)
    if (legacyCase && !localStorage.getItem(currentCaseKey())) {
      localStorage.setItem(currentCaseKey(), legacyCase)
      localStorage.removeItem(CURRENT_CASE_KEY)
    }
  } catch {
    /* 存储不可用时跳过迁移 */
  }
}

function loadHistory(): ChatSession[] {
  try {
    const raw = localStorage.getItem(historyKey())
    const arr = raw ? (JSON.parse(raw) as ChatSession[]) : []
    const sessions = Array.isArray(arr)
      ? arr.filter((s) => s && typeof s.id === 'string' && Array.isArray(s.messages)).slice(0, HISTORY_MAX)
      : []
    // 应用重启后 Core 子进程随之结束，遗留的未终态任务不可能再推进：标记为已取消
    return sessions.map((s) => ({ ...s, messages: cancelNonTerminal(s.messages) }))
  } catch {
    /* 忽略损坏的历史记录 */
    return []
  }
}

function saveHistory(sessions: ChatSession[]): void {
  try {
    localStorage.setItem(historyKey(), JSON.stringify(sessions))
  } catch {
    /* 存储不可用/容量不足时跳过持久化 */
  }
}

/** 快照消息用于持久化：未决审批不保留；render_html 完整 HTML 不落盘 */
function snapshotMessages(messages: Msg[]): Msg[] {
  return messages.map((m) => {
    if (m.role !== 'task') return m
    // render_html 完整 HTML 不持久化（体积大，localStorage 有限）；
    // previewUrl 保留 —— 配合 Main 磁盘持久化实现历史会话预览恢复
    const chat = m.chat?.map((c) => (c.html !== undefined ? { ...c, html: undefined } : c))
    if (TERMINAL.includes(m.lifecycle)) {
      return { ...m, chat, approval: m.approval?.decided ? m.approval : undefined, lastSeq: undefined }
    }
    // 未终态任务：保留生命周期与事件游标 —— 用户切走后任务在 Core 后台继续执行，
    // 事件由后台路径持续写入会话快照；回到会话时据此续挂订阅增量补齐
    return { ...m, chat }
  })
}

/** 应用关闭/重启场景：未终态任务不可能再推进，标记为已取消（内容不完整） */
function cancelNonTerminal(messages: Msg[]): Msg[] {
  return messages.map((m) => {
    if (m.role !== 'task' || TERMINAL.includes(m.lifecycle)) return m
    return {
      ...m,
      chat: m.chat?.map((c) => (c.html !== undefined ? { ...c, html: undefined } : c)),
      lifecycle: 'CANCELLED',
      streamIncomplete: true,
      approval: m.approval?.decided ? m.approval : undefined,
      // 重启后挂起的待答问题集已失效（Core 侧子进程结束），不能继续展示可点的卡片
      questionSet: m.questionSet?.status === 'pending' ? { ...m.questionSet, status: 'cancelled' as const } : m.questionSet,
      lastSeq: undefined
    }
  })
}

const layoutKey = (mode: ModeKey): string => `lawbuddy.layout.${mode}`

function defaultLayout(): LayoutSnap {
  return { leftW: DEF_LEFT, rightW: DEF_RIGHT, leftHide: false, rightHide: false, rightView: 'list' }
}

function loadLayout(mode: ModeKey): LayoutSnap {
  migrateLegacyStorage()
  try {
    const raw = localStorage.getItem(layoutKey(mode))
    if (raw) return { ...defaultLayout(), ...(JSON.parse(raw) as Partial<LayoutSnap>) }
  } catch {
    /* 忽略损坏的本地布局 */
  }
  return defaultLayout()
}

let toastSeq = 1
let msgSeq = 1

/** 消息 id：时间戳前缀 + 自增序号。仅用自增计数器会在应用重启/打开历史会话后与旧消息 id 撞车，
 * 事件归约与停止操作按 id 匹配会同时命中两条消息（表现为新旧任务进度串线、停止"无效"） */
function newMsgId(): string {
  return `m${Date.now().toString(36)}-${msgSeq++}`
}

// ---------------------------------------------------------------------------
// 浏览器调试模式 fallback：AI 演示回复（Electron 下走真实 Agent 任务）
// ---------------------------------------------------------------------------

/** 思考强度显示名（输入坞「模型」设置与提示共用；max 仅部分模型支持） */
export const THINKING_LABEL: Record<ReasoningEffort, string> = {
  off: '关闭',
  low: '轻量',
  medium: '标准',
  high: '深度',
  max: '最强'
}

function makeAiReply(mode: ModeKey, docs: DocMeta[]): AiMessageBody {
  const pick = (i: number): { documentId: string; pageNo: number; label: string; quote: string } | null => {
    const d = docs[i]
    if (!d) return null
    const page = Math.min(i + 1, d.pages || 1)
    return { documentId: d.id, pageNo: page, label: `${d.name}${d.pages > 1 ? ` · 第${page}页` : ''}`, quote: '' }
  }
  const cites = [pick(0), pick(1), pick(2)].filter(
    (x): x is { documentId: string; pageNo: number; label: string; quote: string } => x !== null
  )

  // 法律阅卷：全规格报告（浏览器调试模式的演示数据）
  if (mode === 'yj') {
    const cite0 = cites[0]
    return {
      report: {
        overview: '根据当前卷宗材料，本案系民间借贷纠纷，原告主张被告偿还借款本金及利息，被告对收款事实无异议但抗辩已部分现金归还。',
        parties: [
          { name: '张三', role: '原告', note: '出借人，已履行款项交付义务', cite: cite0 ?? undefined },
          { name: '李四', role: '被告', note: '借款人，主张已现金归还20万元' }
        ],
        timeline: [
          { date: '2023-03-15', event: '双方签订《借款合同》，约定借款100万元、年利率15.4%', cite: cite0 ?? undefined },
          { date: '2024-03-14', event: '借款期限届满，被告未履行还款义务' }
        ],
        disputes: [
          { title: '现金还款认定', desc: '被告主张的20万元现金还款无转账凭证佐证，举证责任在被告', cite: cite0 ?? undefined },
          { title: '违约金调整', desc: '约定违约金标准是否过高应否酌减' }
        ],
        facts: [
          { text: '借款合同与转账凭证相互印证，本金交付证据充分', stance: 'favorable', status: 'confirmed', cite: cite0 ?? undefined },
          { text: '被告主张已现金归还20万元', stance: 'unfavorable', status: 'pending' }
        ],
        contradictions: [
          { topic: '还款事实', sideA: '原告：被告未归还任何本金', sideB: '被告：已现金归还20万元', note: '需调取还款凭证核实' }
        ],
        missing: ['被告财产线索清单（待调查令调取）'],
        legalBasis: [{ title: '民法典第六百七十六条', point: '逾期利息按约定或国家规定支付' }],
        nextSteps: ['申请调查令调取被告银行流水', '整理微信催款记录作为债务确认补强证据']
      },
      cites: cites.map((c, i) => ({ ...c, claimStatus: i === 1 ? 'needs_human' : 'supported', reasonCode: i === 1 ? 'LOW_OCR_CONFIDENCE' : null })),
      audit: '以上为 AI 分析意见，关键事实请以原文为准 · 1 项待人工核实'
    }
  }

  const bodies: Record<Exclude<ModeKey, 'yj'>, Omit<AiMessageBody, 'cites'>> = {
    ht: {
      intro: '已从本方立场完成主合同逐条审查，发现 2 项需要优先处理的风险：',
      points: [
        { title: '违约金标准偏高', desc: '逾期每日万分之五折合年化约 18.25%，建议与利息合并评估上限；' },
        { title: '管辖条款单方有利', desc: '约定由对方所在地法院管辖，对本方应诉便利性不利。' }
      ],
      audit: '以上为 AI 分析意见，修改建议需律师确认后写入工作副本 · 原件不受影响'
    },
    ws: {
      intro: '已按模板整理提纲，正文所需的关键字段核对如下：',
      points: [
        { title: '已确认事实', desc: '可从卷宗定位的事实将逐项附原文引证；' },
        { title: '待补充字段', desc: '「送达地址」等缺失字段将以 xx 标注，不自行编造。' }
      ],
      audit: '生成文书默认为草稿状态，阻断项清零并经确认后方可导出正式稿'
    }
  }
  return { ...bodies[mode], cites }
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

const STAGE_LABEL: Record<string, string> = {
  plan: '生成分析计划',
  retrieve: '检索案件材料',
  analyze: '组织分析结论',
  verify: '绑定原文引证',
  // 阅卷 Loop 阶段（05-阅卷Agent升级方案 §4）
  understand: '理解任务目标',
  corpus: '确认案件范围',
  read: '全案通读',
  synthesize: '建立案件记忆',
  resume: '从检查点恢复',
  quick_reply: '即时回复'
}

/** 脱敏档位文案（REQ-PRIV-002 / 技术方案 11.3；档位定义与 Core POLICY_TYPES 对齐） */
export const POLICY_LABEL: Record<string, string> = {
  light: '轻度',
  standard: '标准',
  strict: '严格',
  custom: '自定义'
}

/** 敏感字段类型清单（与 Core cases.ENTITY_TYPE_ORDER 一致；脱敏仅针对个人敏感信息，不含机构名/案号/信用代码） */
export const ENTITY_TYPES: Array<{ key: string; label: string }> = [
  { key: 'id_card', label: '身份证号' },
  { key: 'bank_card', label: '银行卡号' },
  { key: 'phone', label: '手机号' },
  { key: 'email', label: '邮箱' },
  { key: 'plate', label: '车牌号' },
  { key: 'name', label: '姓名' },
  { key: 'address', label: '地址' },
  { key: 'custom', label: '自定义敏感词' }
]

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

interface AppState {
  // —— 模式与对话 ——
  mode: ModeKey
  /** 法律阅卷工作流预设（PRD 13 四类功能）；null = 未显式选择，由 Agent 按用户意图自主推断 */
  preset: PresetKey | null
  chatting: boolean
  messages: Msg[]
  /** 历史对话会话（当前实现为本地 localStorage 持久化） */
  chatSessions: ChatSession[]
  /** 当前对话对应的会话 id（从历史打开或已保存的会话） */
  activeSessionId: string | null
  /** 当前对话的 Core 会话锚点（V26）：首轮任务发起后由 Core 回填；同会话续聊续传 */
  conversationId: string | null
  input: string
  recording: boolean
  // —— 输入坞选择 ——
  model: string
  /** 当前模型的思考强度（输入坞「模型」设置；off 表示不下发档位参数） */
  thinking: ReasoningEffort
  /** 每个模型各自记住的思考强度（键 = 模型 id）：切换模型不互相覆盖 */
  thinkingByModel: Record<string, ReasoningEffort>
  /**
   * 各任务模式各自记住的模型与思考强度（键 = 模式）。
   * 合同审查首次进入时继承法律阅卷当前选择；之后两边各自记忆、互不覆盖。
   */
  modeSelection: Partial<Record<ModeKey, { model: string; thinking: ReasoningEffort }>>
  /** 当前工作空间根目录（真实路径，由 Core 返回；未知时为空串） */
  space: string
  openPop: string | null
  // —— 技能（/ 菜单；REQ-AI-003） ——
  /** 已安装技能（工作空间级） */
  skills: SkillInfo[]
  /** 本次任务待绑定的技能 id（/ 菜单选中，发送后清空） */
  pendingSkills: string[]
  // —— 案件（真实数据） ——
  cases: CaseInfo[]
  archivedCases: CaseInfo[]
  currentCaseId: string | null
  casePanelOpen: boolean
  creatingCase: boolean
  /** 待永久删除的案件（确认对话框打开时非空） */
  deletingCase: CaseInfo | null
  // —— 文档（真实数据） ——
  documents: DocMeta[]
  importing: boolean
  /** 批量导入进度（Main 分批推送）；null 表示未在导入 */
  importProgress: { done: number; total: number; current?: string } | null
  pendingAttachments: Array<ChatAttachment & { id: string }>
  docPages: Record<string, string[]>
  /** 每文档页元数据（O-008：quality/置信度决定更正入口，corrected 显示「已更正」徽标） */
  docPageMeta: Record<string, Array<OcrPageMeta>>
  /** 每文档批注（DEV-DOC-010：派生数据，quote_missing 读取时由 Core 计算） */
  docAnnotations: Record<string, DocumentAnnotation[]>
  curFileId: string | null
  openTabs: string[]
  rightView: RightView
  zoom: number
  /** 原件真实预览 / 提取文本（默认为原件预览） */
  previewMode: PreviewMode
  fullscreen: boolean
  docSearchOpen: boolean
  docSearchTerm: string
  citeTarget: { docId: string; pageNo: number; nonce: number; quote?: string; documentVersionId?: string } | null
  // —— 产物概览弹窗 ——
  artOpen: boolean
  artPinned: boolean
  artExpanded: boolean
  // —— 审计查看器 ——
  auditOpen: boolean
  // —— 待核实队列（PRD 11.4 / R-007 / R-008） ——
  claimsQueueOpen: boolean
  pendingClaims: ClaimSummary[]
  staleClaims: ClaimSummary[]
  conflictClaims: ClaimSummary[]
  claimsQueueCount: number
  claimsLoading: boolean
  // —— 案件记忆（DEV-AGT-007 / M-001~M-006） ——
  memoryOpen: boolean
  memories: MemoryEntry[]
  memoryLoading: boolean
  // —— AI 产物（M2 收尾：概览 / 内容 / 导出） ——
  artifacts: ArtifactMeta[]
  artifactBodies: Record<string, AiMessageBody>
  // —— 模型 API Key ——
  /** 当前服务商是否已可用密钥（内置默认键也算；不含密钥明文） */
  apiKeyConfigured: boolean
  apiKeyDialogOpen: boolean
  /** 当前服务商 ID（Core settings model.provider） */
  modelProvider: string
  /** 模型目录（Core model.catalog 下发：内置预设 + 自定义服务商与模型能力） */
  modelCatalog: ModelCatalog | null
  /** 待解锁的加密文档（REQ-DOC-004：密码经专用窗口输入） */
  unlockTarget: { documentId: string; title: string } | null
  // —— 脱敏设置（M3 / REQ-PRIV-004） ——
  redactionDialogOpen: boolean
  /** 技能库管理对话框（技能查看与删除） */
  skillDialogOpen: boolean
  // —— 合同审查发起（M3 / REQ-CON：主合同 / 审查方 / 风险偏好） ——
  contractStartOpen: boolean
  wordEditingDirty: boolean
  contractOverviewId: string | null
  bestPracticeTemplateId: string | null
  /** 文书起草选中模板的文书类型（随自然语言任务下发，供 Core 兜底建草稿） */
  docgenDocType: string | null
  // —— 辩护策略立场确认（REQ-STR-001 放宽：输入未明确立场时先询问） ——
  strategyStartOpen: boolean
  // —— 文书生成（DEV-MOD-007/008：发起对话框 + 右侧通用文档预览） ——
  docgenStartOpen: boolean
  /** 当前打开的文书草稿（null = 显示案件文书列表） */
  docgenDraftId: string | null
  // —— 布局 ——
  leftW: number
  rightW: number
  leftHide: boolean
  rightHide: boolean
  // —— 通知 ——
  toasts: ToastItem[]

  // actions —— 数据
  initFromCore: () => Promise<void>
  loadCases: () => Promise<void>
  setCurrentCase: (id: string | null) => Promise<void>
  createCase: (form: NewCaseForm) => Promise<boolean>
  archiveCase: (id: string) => Promise<void>
  restoreCase: (id: string) => Promise<void>
  setDeletingCase: (c: CaseInfo | null) => void
  deleteCase: (id: string) => Promise<boolean>
  importFiles: (mode?: 'file' | 'folder') => Promise<void>
  /** 文件选择器挑选随问附件（图片/文本；图片按当前模型视觉能力门禁） */
  addAttachmentsFromPicker: () => Promise<void>
  /** 剪贴板截图 → 随问附件；当前模型不支持图片理解时提示并拒绝 */
  addClipboardImageAttachment: () => Promise<void>
  removeAttachment: (id: string) => void
  refreshDocuments: () => Promise<void>
  ensurePages: (docId: string) => Promise<string[]>
  /** 脱敏等原地更新后丢弃页文本缓存，下次访问重拉（打码结果立即可见） */
  invalidatePages: (docIds: string[]) => void
  /** OCR 人工更正（O-008）：写更正记录并失效不可定位的引用，返回受影响 Claim 数 */
  correctOcr: (documentId: string, pageNo: number, correctedText: string, note?: string) => Promise<number>
  /** 页批注（DEV-DOC-010）：加载文档批注（含 quote_missing 标记） */
  ensureAnnotations: (docId: string) => Promise<void>
  addAnnotation: (documentId: string, pageNo: number, body: string, quote?: string, color?: AnnotationColor) => Promise<boolean>
  updateAnnotation: (documentId: string, annotationId: string, body: string, color?: AnnotationColor) => Promise<boolean>
  setAnnotationResolved: (documentId: string, annotationId: string, resolved: boolean) => Promise<boolean>
  deleteAnnotation: (documentId: string, annotationId: string) => Promise<boolean>
  getRenderDoc: (id: string) => RenderDoc | undefined
  loadArtifacts: () => Promise<void>
  ensureArtifactBody: (artifactId: string) => Promise<void>
  exportArtifact: (artifactId: string, format: ArtifactExportFormat, title: string) => Promise<void>
  /** 证据卷宗装订：返回结果（供 UI 展示失败清单），取消返回 null */
  exportBundle: (documentIds: string[]) => Promise<EvidenceBundleResult | null>
  // actions —— UI
  toast: (text: string) => void
  setMode: (m: ModeKey) => void
  setPreset: (p: PresetKey) => void
  setInput: (v: string) => void
  /** 输入坞聚焦信号（自增计数；Dock 监听后 focus 输入框，供「问AI」加载选中文本后使用） */
  inputFocusTick: number
  focusInput: () => void
  send: () => void
  restart: () => void
  /** 打开会话（跨案件时先切到所属案件；未终态任务重挂后台订阅续看进度） */
  openSession: (id: string) => Promise<void>
  /** 删除一条历史对话记录 */
  deleteSession: (id: string) => void
  /** 预览卡注册成功后回写地址（工具气泡条目按 callId 关联；历史会话恢复预览的关键线索） */
  attachPreviewUrl: (callId: string, url: string) => void
  toggleRecording: () => void
  setModel: (m: string) => void
  setThinking: (v: ReasoningEffort) => void
  /** 加载技能清单（/ 菜单数据源；安装技能后重取） */
  loadCapabilities: () => Promise<void>
  /** 切换本次任务待绑定技能（/ 菜单选中） */
  togglePendingSkill: (id: string) => void
  /** 选择并切换到其他本地工作空间目录（系统目录选择框） */
  openWorkspacePicker: () => Promise<void>
  /** 切换到已确定的工作空间根目录，并重载该空间的案件、文档与对话 */
  applyWorkspace: (rootUri: string) => Promise<void>
  setOpenPop: (p: string | null) => void
  setCasePanelOpen: (v: boolean) => void
  setCreatingCase: (v: boolean) => void
  openDoc: (id: string) => void
  selectFile: (id: string) => void
  closeTab: (id: string) => void
  showList: () => void
  backToList: () => void
  setZoom: (z: number) => void
  /** 阅读视图呈现方式：render = 按原件格式真实预览；text = Core 提取的页文本 */
  setPreviewMode: (m: PreviewMode) => void
  setFullscreen: (v: boolean) => void
  setDocSearch: (open: boolean, term?: string) => void
  openCitation: (docId: string, pageNo: number) => void
  openCitationTarget: (target: CitationTarget) => Promise<void>
  toggleArt: () => void
  closeArt: (force?: boolean) => void
  toggleArtPin: () => void
  toggleArtExpanded: () => void
  setAuditOpen: (v: boolean) => void
  setClaimsQueueOpen: (v: boolean) => void
  openClaimsQueue: () => void
  loadClaimsQueue: () => Promise<void>
  verifyClaim: (claimId: string, decision: 'confirmed' | 'rejected', note?: string) => Promise<void>
  recheckClaim: (claimId: string) => Promise<void>
  resolveConflict: (claimId: string, decision: 'confirmed' | 'rejected', note?: string) => Promise<void>
  setMemoryOpen: (v: boolean) => void
  loadMemories: () => Promise<void>
  /** 更正记忆：新值生效，原值保留（M-004） */
  correctMemory: (memoryId: string, value: string, note?: string) => Promise<void>
  /** 删除记忆：不再参与召回与上下文装配（M-005） */
  forgetMemory: (memoryId: string) => Promise<void>
  deleteDocument: (docId: string) => Promise<void>
  /** 批量删除文档：逐份调用删除接口，单份失败不中断，结束后汇总提示 */
  deleteDocuments: (docIds: string[]) => Promise<void>
  setApiKeyDialogOpen: (v: boolean) => void
  /** 按服务商保存 API Key（空串清除）；不传 providerId 时用当前服务商 */
  setApiKey: (value: string, providerId?: string) => Promise<void>
  /** 加载模型目录（服务商 / 模型能力 / 密钥 configured 状态） */
  loadModelCatalog: () => Promise<void>
  /** 保存服务商配置（内置写覆盖 / 自定义新增更新），可同时提交 API Key */
  saveModelProvider: (req: SaveProviderRequest) => Promise<boolean>
  /** 删除自定义服务商 */
  removeModelProvider: (providerId: string) => Promise<void>
  /** 恢复内置服务商官方预设 */
  resetModelProvider: (providerId: string) => Promise<void>
  /** 切换模型服务商（持久化到 settings model.provider） */
  setModelProvider: (v: string) => Promise<void>
  /** 打开加密文档解锁窗口（REQ-DOC-004） */
  openUnlock: (documentId: string, title: string) => void
  closeUnlock: () => void
  /** 提交密码解锁；返回是否成功（失败原因经 toast 呈现） */
  unlockDocument: (password: string) => Promise<boolean>
  setContractStartOpen: (v: boolean) => void
  /** 合同审查发起对话框确认：直接发起任务（自主 Loop 由模型自主决策流程） */
  startContractTask: (text: string, inp: ContractStartInput) => void
  /** 辩护策略立场确认对话框开关（输入未明确立场时弹出询问） */
  setStrategyStartOpen: (v: boolean) => void
  /** 立场确认后发起策略任务（stance 随本次任务生效，不回写案件设置） */
  startStrategyTask: (stance: string) => void
  /** 发起阅卷任务 */
  runReviewTask: (text: string, preset: PresetKey | null, stance?: string) => void
  /** 发起合同审查任务 */
  runContractTask: (text: string, inp: ContractStartInput) => Promise<void>
  setDocgenStartOpen: (v: boolean) => void
  /** 发起文书生成（PRD 15.2 输入表经 agent.start.docgenInput 通道）；返回错误供对话框确认重发 */
  startDocGenTask: (text: string, inp: DocGenStartInput) => Promise<{ ok: boolean; error?: string }>
  /** 发起文书 AI 修订（交付物迭代改写）：以草稿当前稿为基准按意见改写，落 ai_revise 版本 */
  startDocRevision: (draftId: string, instruction: string) => Promise<{ ok: boolean; error?: string }>
  /** 在中间区打开文书工作台（draftId 为空回到文书起草首页） */
  openDocGen: (draftId: string | null) => void
  setRedactionDialogOpen: (v: boolean) => void
  /** 打开/关闭技能库管理对话框 */
  setSkillDialogOpen: (v: boolean) => void
  setCaseRedactionPolicy: (policy: RedactionPolicy, customTypes: string[]) => Promise<boolean>
  respondApproval: (msgId: string, approvalId: string, decision: string) => Promise<void>
  /** 回答文书生成的人机决策问题：写入 user_confirmed 并从同一检查点恢复任务 */
  answerHumanQuestions: (msgId: string, taskId: string, questionSetId: string, answers: HumanAnswer[]) => Promise<boolean>
  /** 补拉任务的待答问题集（刷新/重启后恢复问题卡） */
  loadPendingQuestions: (msgId: string, taskId: string) => Promise<void>
  stopTask: (msgId: string, taskId: string) => Promise<void>
  /** 任务结束后重新编辑（停止/完成/失败均适用）：把该轮任务对应的消息文本回填输入坞，修改后可重新发起 */
  restartTask: (msgId: string) => void
  decideStrategy: (taskId: string, planTitle: string) => Promise<boolean>
  setLeftW: (w: number) => void
  setRightW: (w: number) => void
  toggleLeft: () => void
  toggleRight: () => void
  resetLeft: () => void
  resetRight: () => void
}

const toDocMeta = (m: {
  id: string
  caseId?: string
  title: string
  kind: string
  createdAt: string
  pageCount: number | null
  status: string
  ocrPendingPages?: number
  isRedacted?: boolean
}): DocMeta => ({
  id: m.id,
  name: m.kind === 'md' || m.kind === 'txt' ? `${m.title}.${m.kind}` : `${m.title}`,
  type: m.kind,
  date: (m.createdAt || '').slice(0, 10),
  pages: m.pageCount ?? 1,
  status: m.status,
  ocrPending: m.ocrPendingPages ?? 0,
  caseId: m.caseId,
  isRedacted: !!m.isRedacted
})

// ---------------------------------------------------------------------------
// 任务事件消费（REQ-AI-001）：Core 推送（onAgentEvent）实时到达，
// 2s 轮询兜底漏推/重连（afterSeq 续订）；两路共用 applyTaskEvents，按 seq 去重
// ---------------------------------------------------------------------------

type TaskMsg = Extract<Msg, { role: 'task' }>
/** 任务终态生命周期（任务不再推进；侧栏会话「执行中」徽标也据此判断） */
export const TERMINAL = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'CANCELLED', 'FAILED']

/** 运行中的任务消息（Dock 发送按钮双态依据：存在即显示停止按钮） */
export function findRunningTaskMsg(messages: Msg[]): TaskMsg | undefined {
  return messages.find((m): m is TaskMsg => m.role === 'task' && !TERMINAL.includes(m.lifecycle))
}
/** 活跃任务订阅（taskId → 退订函数）；终态/重启时清理，防 preload 泄漏 */
const taskFeeds = new Map<string, () => void>()

/** 工具参数/结果的展示摘要（截断，避免消息流与本地持久化膨胀） */
function clipPreview(value: unknown, limit = 600): string {
  let text: string
  if (typeof value === 'string') text = value
  else {
    try {
      text = JSON.stringify(value)
    } catch {
      text = String(value)
    }
  }
  return text.length > limit ? text.slice(0, limit) + '…' : text
}

/**
 * 从输入中识别明确的辩护立场（REQ-STR-001 放宽）：
 * 「为被告生成一份辩护策略」「站在原告立场」「中立分析」等直接命中；
 * 仅泛指案件当事人（如「被告的主张是什么」）不触发。未命中返回 null。
 */
const STANCE_RE =
  /(?:为|代表|站在|作为|我是|我方是|以)(原告|被告|上诉人|被上诉人|申请人|被申请人|第三人)|(原告|被告|上诉人|被上诉人|申请人|被申请人|第三人)(?:方|立场|角度|视角|一侧)|中立/

function detectStance(text: string): string | null {
  const m = text.match(STANCE_RE)
  if (!m) return null
  return m[1] ?? m[2] ?? '中立'
}

/** 内联短片段截断（中文摘要用） */
function snip(value: unknown, limit = 40): string {
  const text = String(value ?? '').trim().replace(/\s+/g, ' ')
  return text.length > limit ? text.slice(0, limit) + '…' : text
}

/**
 * 工具参数 → 中文动作摘要（用户不读原始 JSON）。
 * 未收录的工具退回 JSON 截断预览，保证新工具有基本可读性。
 */
function describeToolArgs(name: string, args: unknown): string {
  const a = (typeof args === 'object' && args !== null ? args : {}) as Record<string, unknown>
  const s = (k: string): string => String(a[k] ?? '').trim()
  const n = (k: string): number | undefined => (typeof a[k] === 'number' ? (a[k] as number) : undefined)
  switch (name) {
    case 'document_search':
    case 'memory_search':
      return `检索「${snip(s('query'))}」`
    case 'document_get_pages': {
      const from = n('pageFrom') ?? 1
      const to = n('pageTo') ?? from
      return `读取《${snip(s('documentId'), 24)}》第 ${from}${to !== from ? `–${to}` : ''} 页`
    }
    case 'task_plan': {
      const steps = Array.isArray(a.steps) ? a.steps.length : 0
      return `更新任务清单（共 ${steps} 步）`
    }
    case 'case_explore':
      return `委托调查「${snip(s('question'), 60)}」`
    case 'memory_write':
      return `写入案件记忆「${snip(s('key') || s('kind'), 20)}」`
    case 'contract_create_review':
      return `创建合同审查${s('contractType') ? `（${snip(s('contractType'), 20)}）` : ''}`
    case 'contract_split_clauses':
      return '切分合同条款并做确定性检查'
    case 'contract_get_clauses':
      return '读取合同条款'
    case 'request_user_decision': {
      const count = Array.isArray(a.questions) ? a.questions.length : 0
      return `等待你回答 ${count} 项关键决策`
    }
    case 'docgen_create_draft':
      return `创建文书草稿「${snip(s('docType'), 20)}」`
    case 'skill_install':
      return '安装技能'
    case 'skill_remove':
      return '删除技能'
    case 'read_file':
      return `读取文件 ${snip(s('path'), 36)}`
    case 'write_file':
      return `写入文件 ${snip(s('path'), 36)}`
    case 'edit_file':
      return `编辑文件 ${snip(s('path'), 36)}`
    case 'glob_files':
      return `查找文件 ${snip(s('pattern'), 30)}`
    case 'grep_search':
      return `搜索内容「${snip(s('pattern'), 30)}」`
    case 'bash':
      return `执行命令：${snip(s('command'), 60)}`
    case 'render_pptx': {
      const pages = Array.isArray(a.slides) ? a.slides.length : 0
      const label = s('fileName') || s('title')
      return `生成 PPT${label ? `「${snip(label, 24)}」` : ''}（${pages} 页）`
    }
    default:
      return clipPreview(args)
  }
}

/** 工具结果 → 中文结果摘要（同上，未收录工具退回 JSON 截断预览） */
function describeToolResult(name: string, result: unknown): string {
  const r = (typeof result === 'object' && result !== null ? result : {}) as Record<string, unknown>
  if (typeof r.error === 'string' && r.error) return `出错：${snip(r.error, 120)}`
  const count = (k: string): number => (Array.isArray(r[k]) ? (r[k] as unknown[]).length : 0)
  switch (name) {
    case 'documents_list':
      return `返回 ${count('documents')} 份文档`
    case 'document_search':
      return `命中 ${count('hits')} 处`
    case 'document_get_pages':
      return `返回 ${count('pages')} 页文本`
    case 'memory_search':
      return `命中 ${count('memories')} 条记忆`
    case 'memory_write':
      return '已写入案件记忆'
    case 'task_plan':
      return '任务清单已更新'
    case 'case_explore': {
      const reads = Number(r.reads ?? 0)
      const searches = Number(r.searches ?? 0)
      const concl = snip(r.conclusion, 160)
      return `已读取 ${reads} 个文件 · 检索 ${searches} 次${concl ? `：${concl}` : ''}`
    }
    case 'render_html':
      return '已生成预览卡片'
    case 'request_user_decision':
      return r.paused === true ? '已暂停，等待你的回答' : '已提交决策问题'
    case 'render_pptx':
      return `已生成 PPTX 文件（${Number(r.slides ?? 0)} 页）`
    default:
      return clipPreview(result)
  }
}

/** render_html 预览注册（事件到达即注册，不等卡片挂载）：
 *  快照会剥离完整 HTML，如果只靠预览卡挂载时才注册，任务在后台完成、切换会话或
 *  重载恢复后就没有 previewUrl，图会永久消失（模型已出图但用户看不到）。 */
const pendingPreviewRegs = new Set<string>()

function registerRenderPreviews(chat?: TaskChatItem[]): void {
  if (!chat) return
  const bridge = getBridge()
  if (!bridge) return
  for (const c of chat) {
    if (c.kind !== 'tool' || c.name !== 'render_html' || !c.html || c.previewUrl) continue
    const callId = c.callId || ''
    if (!callId || pendingPreviewRegs.has(callId)) continue
    pendingPreviewRegs.add(callId)
    void bridge.preview
      .register(c.html)
      .then((url) => {
        useApp.getState().attachPreviewUrl(callId, url)
        upsertSession(false) // 立刻落盘 previewUrl，避免终态快照早于注册完成而丢失
      })
      .catch(() => pendingPreviewRegs.delete(callId))
  }
}

/** 将一批事件归约进任务消息（推送与轮询重复投递安全；当前对话与后台会话快照共用） */
function reduceTaskEvents(m: TaskMsg, fresh: AgentTaskEvent[]): TaskMsg {
  let steps = [...m.steps]
  let chat = m.chat ?? []
  let turn = m.turn
  let lifecycle = m.lifecycle
  let body = m.body
  let error = m.error
  let approval = m.approval
  let streamText = m.streamText
  let streamPreview = m.streamPreview
  let streamIncomplete = m.streamIncomplete
  let thinkingChars = m.thinkingChars
  let cites = m.cites
  let taskPlan = m.taskPlan
  let questionSet = m.questionSet
  let lastSeq = m.lastSeq ?? 0
  for (const e of fresh) {
    const p = e.payload as Record<string, unknown>
    if (e.type === 'status') {
      lifecycle = String(p.lifecycle ?? lifecycle)
      const stageLabel = p.stage ? STAGE_LABEL[String(p.stage)] : undefined
      if (stageLabel) {
        steps = steps.map((s) => ({ ...s, state: 'done' as const }))
        // 同阶段多次推进（全案通读每批推进一次）不重复压栈，只保持活跃
        const last = steps[steps.length - 1]
        if (last && last.label === stageLabel) {
          steps[steps.length - 1] = { ...last, state: 'active' as const }
        } else {
          steps.push({ label: stageLabel, state: 'active' as const })
        }
      }
    } else if (e.type === 'text') {
      // 自主 Loop：文本增量（追加式拼接，节流批量到达）
      streamText = (streamText ?? '') + String(p.delta ?? '')
    } else if (e.type === 'tool_use_progress') {
      // 工具参数流式生成中：建占位气泡显示进度（防「假卡死」）；
      // 同 callId 的 tool_use_start 到达后原地合并为正式条目
      const callId = String(p.callId ?? '')
      const name = String(p.name ?? '')
      const chars = Number(p.chars ?? 0)
      const label = name === 'render_html' ? '正在生成 HTML' : name === 'render_pptx' ? '正在生成幻灯片' : '正在生成调用参数'
      const idx = chat.findIndex((c) => c.kind === 'tool' && c.callId === callId)
      if (idx >= 0) {
        chat = chat.map((c) => (c.kind === 'tool' && c.callId === callId ? { ...c, args: `${label}… ${chars} 字符` } : c))
      } else {
        chat = [...chat, { kind: 'tool', callId, name, args: `${label}… ${chars} 字符`, status: 'running' as const }]
      }
    } else if (e.type === 'tool_use_start') {
      // 显示「正在调用工具」；tool_use_done 到达后原地更新状态。
      // 若此前已有同 callId 的生成进度占位（tool_use_progress），原地合并而非追加
      const callId = String(p.callId ?? '')
      const name = String(p.name ?? '')
      // render_html：完整 HTML 需送前端预览卡，不走 600 字截断
      const rawArgs = (p.arguments ?? {}) as Record<string, unknown>
      const isRender = name === 'render_html'
      const patch = {
        name,
        args: isRender ? '(HTML 文档)' : describeToolArgs(name, p.arguments),
        html: isRender && typeof rawArgs.html === 'string' ? rawArgs.html : undefined,
        title: isRender && typeof rawArgs.title === 'string' && rawArgs.title.trim() ? rawArgs.title : undefined
      }
      const idx = chat.findIndex((c) => c.kind === 'tool' && c.callId === callId)
      if (idx >= 0) {
        chat = chat.map((c) => (c.kind === 'tool' && c.callId === callId ? { ...c, ...patch } : c))
      } else {
        chat = [...chat, { kind: 'tool', callId, ...patch, status: 'running' as const }]
      }
    } else if (e.type === 'tool_use_done') {
      chat = chat.map((c) =>
        c.kind === 'tool' && c.callId === String(p.callId ?? '')
          ? { ...c, status: p.status === 'ok' ? ('ok' as const) : ('error' as const) }
          : c
      )
    } else if (e.type === 'thinking_progress') {
      // 模型深度思考进行中：仅更新字数（UI 在无正文流式时显示思考进度）
      thinkingChars = Number(p.chars ?? 0)
    } else if (e.type === 'plan_update') {
      // 会话级任务清单（task_plan 工具输出后覆盖式更新；重放按 seq 去重天然幂等）
      const raw = Array.isArray(p.steps) ? p.steps : []
      taskPlan = raw
        .filter((s): s is Record<string, unknown> => typeof s === 'object' && s !== null)
        .map((s) => ({ text: String(s.text ?? ''), status: String(s.status ?? 'pending') }))
        .filter((s) => s.text)
    } else if (e.type === 'explore_progress') {
      // 检索子代理进度：挂到外层工具条目（同 callId 就地更新进度文案）
      const callId = String(p.callId ?? '')
      const note = `已读取 ${Number(p.reads ?? 0)} 个文件 · 检索 ${Number(p.searches ?? 0)} 次`
      chat = chat.map((c) => (c.kind === 'tool' && c.callId === callId ? { ...c, progress: note } : c))
    } else if (e.type === 'assistant_message') {
      // 模型整轮输出定稿：冲刷流式缓冲，追加 assistant 条目
      chat = [...chat, { kind: 'assistant', text: String(p.content ?? ''), round: Number(p.round ?? 0) }]
      streamText = ''
      thinkingChars = 0
    } else if (e.type === 'tool_result_message') {
      chat = [
        ...chat,
        {
          kind: 'toolResult',
          callId: String(p.callId ?? ''),
          name: String(p.name ?? ''),
          status: p.status === 'ok' ? ('ok' as const) : ('error' as const),
          resultPreview: describeToolResult(String(p.name ?? ''), p.result)
        }
      ]
    } else if (e.type === 'turn_complete') {
      // 循环结束：loading 收尾与用时提示由 ChatView 依据 turn 渲染
      const rawFinalize = p.finalize as Record<string, unknown> | undefined
      turn = {
        rounds: Number(p.rounds ?? 0),
        reason: String(p.reason ?? ''),
        forcedFinal: p.forcedFinal === true,
        promptTokens: typeof p.promptTokens === 'number' ? p.promptTokens : undefined,
        completionTokens: typeof p.completionTokens === 'number' ? p.completionTokens : undefined,
        durationMs: typeof p.durationMs === 'number' ? p.durationMs : undefined,
        // 完成守门（05 方案 §7）：旧任务事件无 finalize，按干净完成渲染
        finalize:
          rawFinalize && typeof rawFinalize === 'object'
            ? {
                ok: rawFinalize.ok === true,
                taskDepth: String(rawFinalize.taskDepth ?? ''),
                warnings: Array.isArray(rawFinalize.warnings)
                  ? (rawFinalize.warnings as Array<Record<string, unknown>>).map((w) => ({
                      code: String(w.code ?? ''),
                      text: String(w.text ?? ''),
                      files: Array.isArray(w.files) ? (w.files as string[]) : undefined
                    }))
                  : [],
                coverage: {
                  required: (rawFinalize.coverage as Record<string, unknown> | undefined)?.required === true,
                  totalPages: Number((rawFinalize.coverage as Record<string, unknown> | undefined)?.totalPages ?? 0),
                  readPages: Number((rawFinalize.coverage as Record<string, unknown> | undefined)?.readPages ?? 0),
                  unread: Number((rawFinalize.coverage as Record<string, unknown> | undefined)?.unread ?? 0),
                  excluded: Number((rawFinalize.coverage as Record<string, unknown> | undefined)?.excluded ?? 0)
                }
              }
            : undefined
      }
    } else if (e.type === 'message_delta') {
      // 流式累积文本（覆盖式，重连恢复无需拼接）；preview 为可读成稿片段
      streamText = String(p.text ?? '')
      streamPreview = typeof p.preview === 'string' ? p.preview : undefined
      if (p.incomplete === true) streamIncomplete = true
    } else if (e.type === 'tool_call') {
      steps.push({ label: `检索：${String(p.query ?? '')}`, state: 'active' })
    } else if (e.type === 'tool_result') {
      steps = steps.map((s, idx) =>
        idx === steps.length - 1 ? { ...s, state: 'done' as const, detail: `${p.count ?? 0} 处命中` } : s
      )
    } else if (e.type === 'model_call') {
      // 模型调用属于后台信息，不进入任务执行步骤；仅失败与拦截需要用户感知
      const st = String(p.status)
      if (st === 'blocked') {
        steps.push({ label: '模型调用已拦截', state: 'done', detail: String(p.reason ?? '') })
      } else if (st === 'failed') {
        steps.push({ label: '模型调用失败', state: 'done', detail: String(p.reason ?? '') })
      }
    } else if (e.type === 'budget') {
      // DEV-AGT-006：预算预警/拦截/无进展（A-002/A-003/A-004/A-013）
      const stage = String(p.stage ?? 'warn')
      const scope = String(p.scope ?? '')
      const label =
        stage === 'blocked'
          ? scope === 'repeated_call'
            ? '重复调用已拦截'
            : scope === 'no_progress'
              ? '无进展已停止（NO_PROGRESS）'
              : '预算已耗尽'
          : '预算预警'
      steps.push({
        label,
        state: 'done',
        detail: `${String(p.reason ?? '')}${scope ? ` · ${scope}` : ''}`
      })
    } else if (e.type === 'task_card') {
      // 任务理解卡（阅卷升级方案 §7.1）
      const goal = String((p.card as Record<string, unknown> | undefined)?.goal ?? '')
      steps.push({ label: '任务理解', state: 'done', detail: goal })
    } else if (e.type === 'corpus') {
      steps.push({
        label: `确认案件范围（${Number(p.documents ?? 0)} 份材料 / ${Number(p.units ?? 0)} 个阅读单元）`,
        state: 'done'
      })
    } else if (e.type === 'reading') {
      if (p.stage === 'degraded') {
        steps.push({ label: '通读降级', state: 'done', detail: String(p.reason ?? '') })
      } else {
        const idx = steps.map((s) => s.label).lastIndexOf('全案通读')
        const detail = `已读 ${Number(p.read ?? 0)}/${Number(p.total ?? 0)}（${Number(p.coveragePct ?? 0)}%）`
        if (idx >= 0) steps[idx] = { ...steps[idx], state: 'active', detail }
        else steps.push({ label: '全案通读', state: 'active', detail })
      }
    } else if (e.type === 'case_memory') {
      steps.push({
        label: '案件记忆',
        state: 'done',
        detail: `时间线 ${Number(p.timeline ?? 0)} · 事实 ${Number(p.keyFacts ?? 0)} · 主体 ${Number(p.entities ?? 0)}`
      })
    } else if (e.type === 'case_context') {
      // 文书生成案件上下文准备（只读索引：关联案件 + 材料页数 + 解析状态）
      const shared = Number(p.sharedDocuments ?? 0)
      steps.push({
        label: '了解关联案件',
        state: 'done',
        detail:
          `本任务 ${Number(p.ownDocuments ?? 0)} 份材料` +
          (p.linkedCase ? ` · 只读共享 ${shared} 份` : '') +
          ` · 共 ${Number(p.totalPages ?? 0)} 页` +
          (Number(p.ocrPendingPages ?? 0) ? ` · OCR 待完成 ${Number(p.ocrPendingPages)} 页` : '')
      })
    } else if (e.type === 'verification') {
      const warnings = Array.isArray(p.warnings) ? (p.warnings as string[]).join('；') : ''
      steps.push({
        label: '质量校验',
        state: 'done',
        detail: `通读覆盖 ${String(p.readingCoverage ?? '')}${warnings ? ` · ${warnings}` : ''}`
      })
    } else if (e.type === 'resume') {
      steps.push({ label: '从检查点恢复', state: 'done', detail: `恢复自 ${String(p.fromNode ?? '')} 阶段` })
    } else if (e.type === 'human_intervention') {
      // §11.3：脱敏阻断等人工确认——任务挂起，确认后重新发起从断点续跑
      steps.push({
        label: '需要人工确认',
        state: 'active',
        detail: `${String(p.reason ?? '')}；${String(p.action ?? '')}`
      })
    } else if (e.type === 'approval_request') {
      approval = {
        card: {
          approvalId: String(p.approvalId),
          capability: String(p.capability),
          target: String(p.target),
          dataSummary: String(p.dataSummary),
          payloadHash: String(p.payloadHash),
          expiresInMinutes: Number(p.expiresInMinutes ?? 10),
          fallback: String(p.fallback ?? '')
        }
      }
    } else if (e.type === 'answer_verification') {
      // 收尾引用校验：角标数据源（悬浮出处卡 + 待核实状态），键 = `标题|页码`
      const rows = Array.isArray(p.cites) ? (p.cites as Record<string, unknown>[]) : []
      const next: Record<string, CiteMarkInfo> = { ...(cites ?? {}) }
      for (const r of rows) {
        const title = String(r.title ?? '')
        // 无页码引用（整文档级）页码记 0；区间引用附 pageEnd
        const pageNo = Number(r.pageNo ?? 0)
        if (!title) continue
        next[`${title}|${pageNo}`] = {
          title,
          pageNo,
          pageEnd: r.pageEnd ? Number(r.pageEnd) : undefined,
          documentId: r.documentId ? String(r.documentId) : undefined,
          claimStatus: r.claimStatus ? String(r.claimStatus) : undefined,
          snippet: typeof r.snippet === 'string' ? r.snippet : undefined
        }
      }
      cites = next
    } else if (e.type === 'human_question') {
      // 文书生成需要用户决策：挂载问题集（任务进入 WAITING_HUMAN，等待回答后从检查点恢复）
      const qs = p.questionSet as HumanQuestionSet | undefined
      if (qs && typeof qs.id === 'string') questionSet = qs
    } else if (e.type === 'human_answer') {
      // 用户已提交回答：卡片转为只读摘要（保留当时的问题与选择，便于审计与版本追溯）
      if (questionSet && questionSet.id === String(p.questionSetId ?? '')) {
        const answers = Array.isArray(p.answers) ? (p.answers as HumanAnswer[]) : []
        questionSet = { ...questionSet, status: 'answered', answers, answeredAt: new Date().toISOString() }
      }
    } else if (e.type === 'task_resumed') {
      // 任务从同一检查点恢复续跑：清除挂起状态（回答摘要仍保留）
      if (questionSet && questionSet.id === String(p.questionSetId ?? '')) {
        questionSet = { ...questionSet, status: 'answered' }
      }
    } else if (e.type === 'message') {
      body = p.body as AiMessageBody
    } else if (e.type === 'error') {
      error = String(p.message ?? '任务执行失败')
      const recover = typeof p.recover === 'string' ? p.recover : ''
      if (recover) steps.push({ label: '恢复建议', state: 'done', detail: recover })
    }
    lastSeq = Math.max(lastSeq, e.seq)
  }
  if (TERMINAL.includes(lifecycle)) {
    steps = steps.map((s) => ({ ...s, state: 'done' as const }))
    // 终态后不得残留「执行中」的工具条目：未等到结果的标记为 stopped，避免蓝点继续闪动误导用户
    chat = chat.map((c) => (c.kind === 'tool' && c.status === 'running' ? { ...c, status: 'stopped' as const } : c))
  }
  return {
    ...m,
    lifecycle,
    steps,
    chat,
    turn,
    body,
    error,
    approval,
    streamText,
    streamPreview,
    streamIncomplete,
    thinkingChars,
    cites,
    taskPlan,
    questionSet,
    // 首次到达终态时记录完成时刻（后台批量归约与前台一致，快照持久化保留）
    endedAt: TERMINAL.includes(lifecycle) ? (m.endedAt ?? Date.now()) : m.endedAt,
    lastSeq
  }
}

/** 后台会话快照落盘节流：流式事件高频到达，最多 3s 写一次 localStorage（终态必写） */
let lastBgSave = 0

/** 应用后台会话路径：找到任务消息所属的会话快照并原位更新 */
function applyTaskEventsToSessions(msgId: string, events: AgentTaskEvent[]): void {
  const state = useApp.getState()
  const sess = state.chatSessions.find((x) => x.messages.some((m) => m.id === msgId && m.role === 'task'))
  if (!sess) return
  const bg = sess.messages.find((m): m is TaskMsg => m.id === msgId && m.role === 'task')
  if (!bg || TERMINAL.includes(bg.lifecycle)) return
  const fresh = events.filter((e) => e.seq > (bg.lastSeq ?? 0))
  if (fresh.length === 0) return
  const next = reduceTaskEvents(bg, fresh)
  const terminal = TERMINAL.includes(next.lifecycle)
  // 挂起等回答同样要落盘（问题卡需在刷新/切回会话后恢复），但不属于终态
  const paused = next.lifecycle === 'WAITING_HUMAN'
  const sessions = state.chatSessions.map((x) =>
    x.id === sess.id
      ? {
          ...x,
          // 终态时刷新排序时间，让最近完成的会话排到案件会话列表前面
          updatedAt: terminal ? Date.now() : x.updatedAt,
          messages: x.messages.map((m) => (m.id === msgId ? next : m))
        }
      : x
  )
  useApp.setState({ chatSessions: sessions })
  registerRenderPreviews(next.chat)
  if (terminal || paused || Date.now() - lastBgSave > 3000) {
    lastBgSave = Date.now()
    saveHistory(sessions)
  }
  if (terminal) {
    // 任务属于当前案件时联动刷新队列与产物（其他案件回到会话时再刷新）
    if (sess.caseId && sess.caseId === useApp.getState().currentCaseId) {
      void useApp.getState().loadClaimsQueue()
      void useApp.getState().loadArtifacts()
    }
  }
}

/** 将一批事件归约进对应任务消息：优先当前对话；不在当前对话则更新所属会话快照（后台执行） */
function applyTaskEvents(msgId: string, events: AgentTaskEvent[]): void {
  const cur = useApp.getState().messages.find((m) => m.id === msgId)
  if (!cur || cur.role !== 'task') {
    // 当前对话中不存在（用户已切到其他会话/案件/新对话）：后台续写会话快照
    applyTaskEventsToSessions(msgId, events)
    return
  }
  const fresh = events.filter((e) => e.seq > (cur.lastSeq ?? 0))
  if (fresh.length === 0) return

  const wasTerminal = TERMINAL.includes(cur.lifecycle)
  const next = reduceTaskEvents(cur, fresh)
  useApp.setState((s) => ({
    messages: s.messages.map((m) => (m.id === msgId && m.role === 'task' ? next : m))
  }))
  // 兼容旧任务或事件乱序：等待状态先到而问题卡尚未到达时，主动补拉待答问题集。
  if (next.lifecycle === 'WAITING_HUMAN' && !next.questionSet) {
    void useApp.getState().loadPendingQuestions(msgId, cur.taskId)
  }
  registerRenderPreviews(next.chat)

  if (!wasTerminal && TERMINAL.includes(next.lifecycle)) {
    // 任务到达终态：当前对话增量落盘，让案件会话列表实时可见
    upsertSession(false)
    // 任务产出新 Claim 与产物 → 队列计数 / 产物概览联动刷新（M2 收尾）
    void useApp.getState().loadClaimsQueue()
    void useApp.getState().loadArtifacts()
  } else if (next.lifecycle === 'WAITING_HUMAN' && cur.lifecycle !== 'WAITING_HUMAN') {
    // 挂起等回答：立即落盘问题集，刷新后仍能恢复问题卡
    upsertSession(false)
  }
}

/** 结束任务订阅（终态/重启/上限退出，双路径调用幂等） */
function endFeed(taskId: string): void {
  const off = taskFeeds.get(taskId)
  if (off) {
    off()
    taskFeeds.delete(taskId)
  }
}

/** 订阅任务事件推送 + 兜底轮询（推送正常时轮询几乎无事件；漏推由 afterSeq 补齐） */
async function startTaskFeed(msgId: string, taskId: string): Promise<void> {
  const bridge = getBridge()
  if (!bridge || taskFeeds.has(taskId)) return
  const isTerminal = (): boolean => {
    const st = useApp.getState()
    const m = st.messages.find((x) => x.id === msgId)
    if (m) {
      return m.role !== 'task' || TERMINAL.includes(m.lifecycle) ||
        (m.lifecycle === 'WAITING_HUMAN' && !!m.questionSet)
    }
    // 后台任务：消息已随会话切走，从会话快照判断生命周期
    const sess = st.chatSessions.find((x) => x.messages.some((y) => y.id === msgId))
    const bm = sess?.messages.find((x) => x.id === msgId)
    return !bm || bm.role !== 'task' || TERMINAL.includes(bm.lifecycle) ||
      (bm.lifecycle === 'WAITING_HUMAN' && !!bm.questionSet)
  }
  const off = bridge.agent.onAgentEvent((n) => {
    if (n.taskId !== taskId) return
    applyTaskEvents(msgId, [n.event])
    if (isTerminal()) endFeed(taskId)
  })
  taskFeeds.set(taskId, off)

  const cur = useApp.getState().messages.find((x) => x.id === msgId)
  let afterSeq = cur && cur.role === 'task' ? (cur.lastSeq ?? 0) : 0
  for (let i = 0; i < 900; i++) {
    await sleep(i === 0 ? 300 : 2000) // 首轮快速补齐订阅前的早期事件
    if (isTerminal() || !taskFeeds.has(taskId)) {
      endFeed(taskId)
      return
    }
    let events: AgentTaskEvent[] = []
    try {
      events = await bridge.agent.getEvents({ taskId, afterSeq })
    } catch {
      // RPC notification/增量查询都可能短暂失败；下一轮重试。
      continue
    }
    if (events.length > 0) {
      afterSeq = events[events.length - 1].seq
      applyTaskEvents(msgId, events)
    }
    // 终态对账：即使最后一条 status 通知丢失，也不能让 UI 永久保持转圈。
    // 每 4 轮查询一次，避免增加正常任务的 RPC 压力。
    if (i % 4 === 0) {
      try {
        const task = await bridge.agent.getTask(taskId)
        const current = useApp.getState().messages.find((x) => x.id === msgId)
        if (current?.role === 'task' && task.lifecycle !== current.lifecycle) {
          applyTaskEvents(msgId, [{
            // 合成事件只用于状态归约，使用一个大于游标的本地序号，
            // 避免被 applyTaskEvents 的去重逻辑忽略。
            seq: afterSeq + 1,
            type: 'status',
            payload: { lifecycle: task.lifecycle, stage: task.stage },
            createdAt: new Date().toISOString()
          } as AgentTaskEvent])
          afterSeq += 1
        }
      } catch {
        // 事件轮询仍是主路径，对账失败不阻断下一轮。
      }
    }
    if (isTerminal()) {
      endFeed(taskId)
      return
    }
  }
  endFeed(taskId)
}

export const useApp = create<AppState>((set, get) => {
  /** 当前输入坞选择（模型 + 思考强度），作为模式切换时的继承来源 */
  const currentSelection = (): { model: string; thinking: ReasoningEffort } => ({
    model: get().model,
    thinking: get().thinking
  })

  /**
   * 切换到某模式时套用的模型/思考强度：有该模式自己的记录就用记录，
   * 否则继承切换前的选择（合同审查首次进入因此继承法律阅卷）。
   */
  const selectionForMode = (target: ModeKey): { model: string; thinking: ReasoningEffort } => {
    const s = get()
    const remembered = s.modeSelection[target]
    const source = remembered ?? currentSelection()
    // 记录的模型已从目录移除时回落到当前模型，避免发一次必然失败的请求
    // （目录未加载时不做存在性校验，否则会把记住的模型误判为已下线）
    const known = !s.modelCatalog || (!!source.model && !!findCatalogModel(s.modelCatalog, source.model))
    const model = known ? source.model : s.model
    return { model, thinking: normalizeThinkingForModel(s.modelCatalog, model, source.thinking) }
  }

  /** 模式切换要一并写入的状态：记住旧模式的选择，并套用目标模式的选择 */
  const modeSwitchPatch = (target: ModeKey): Pick<AppState, 'modeSelection' | 'model' | 'thinking'> => {
    const s = get()
    const next = selectionForMode(target)
    return {
      modeSelection: { ...s.modeSelection, [s.mode]: currentSelection(), [target]: next },
      model: next.model,
      thinking: next.thinking
    }
  }

  const persist = (): void => {
    const s = get()
    const snap: LayoutSnap = {
      leftW: s.leftW,
      rightW: s.rightW,
      leftHide: s.leftHide,
      rightHide: s.rightHide,
      rightView: s.rightView
    }
    try {
      localStorage.setItem(layoutKey(s.mode), JSON.stringify(snap))
    } catch {
      /* 存储不可用时跳过持久化 */
    }
  }

  return {
    mode: 'yj',
    preset: null,
    chatting: false,
    messages: [],
    chatSessions: loadHistory(),
    activeSessionId: null,
    conversationId: null,
    input: '',
    inputFocusTick: 0,
    recording: false,
    // 内置模型目录已清空：模型由用户在「模型设置」中添加，启动时由
    // loadModelCatalog 回填；未添加任何模型时保持空串（界面显示引导）
    model: '',
    // 默认「轻量」：GLM 系强制思考无法真正关闭，「关闭」落档 high 会把简单
    // 问候也拖进数分钟深度思考（2026-09-25 实证）；轻量档闲聊秒回、分析够用
    thinking: 'low',
    // 每个模型各自记住的思考强度：切换模型时恢复该模型上次选的档位
    thinkingByModel: {},
    // 各任务模式各自记住的模型与思考强度（合同审查首次进入继承法律阅卷的选择）
    modeSelection: {},
    space: '',
    openPop: null,
    skills: [],
    pendingSkills: [],
    cases: [],
    archivedCases: [],
    currentCaseId: null,
    casePanelOpen: false,
    creatingCase: false,
    deletingCase: null,
    documents: [],
    importing: false,
    importProgress: null,
    pendingAttachments: [],
    docPages: {},
    docPageMeta: {},
    docAnnotations: {},
    curFileId: null,
    openTabs: [],
    // 重启后草稿不随布局持久化：上次停在文书 Word 视图时回落文档列表，避免空白面板
    rightView: (() => {
      const v = loadLayout('yj').rightView
      return v === 'docgen' ? 'list' : v
    })(),
    zoom: 1,
    previewMode: 'render',
    fullscreen: false,
    docSearchOpen: false,
    docSearchTerm: '',
    citeTarget: null,
    artOpen: false,
    artPinned: false,
    artExpanded: false,
    auditOpen: false,
    claimsQueueOpen: false,
    pendingClaims: [],
    staleClaims: [],
    conflictClaims: [],
    claimsQueueCount: 0,
    claimsLoading: false,
    memoryOpen: false,
    memories: [],
    memoryLoading: false,
    artifacts: [],
    artifactBodies: {},
    apiKeyConfigured: false,
    apiKeyDialogOpen: false,
    modelProvider: 'zhipu',
    modelCatalog: null,
    unlockTarget: null,
    redactionDialogOpen: false,
    skillDialogOpen: false,
    contractStartOpen: false,
    wordEditingDirty: false,
    contractOverviewId: null,
    bestPracticeTemplateId: null,
    docgenDocType: null,
    strategyStartOpen: false,
    docgenStartOpen: false,
    docgenDraftId: null,
    ...(() => {
      const l = loadLayout('yj')
      return { leftW: l.leftW, rightW: l.rightW, leftHide: l.leftHide, rightHide: l.rightHide }
    })(),
    toasts: [],

    // ---------------- 初始化与案件 ----------------

    initFromCore: async () => {
      const bridge = getBridge()
      if (!bridge) return
      const run = async (): Promise<void> => {
        const info = await bridge.workspace.ensure()
        // 工作空间决定案件库与对话历史的归属：先切换历史键，再加载数据。
        // Core 崩溃重连时仍是同一目录，不能重置内存中的会话快照
        if (info.rootUri !== get().space) {
          useWorkspaceHistory(info.rootUri)
          set({ space: info.rootUri, chatSessions: loadHistory() })
        }
        // 模型目录（服务商 / 模型能力 / 密钥 configured）由 Core 统一下发；
        // 当前服务商与默认模型随目录一并确定，前端不再硬编码清单
        await get().loadModelCatalog()
        void get().loadCapabilities() // 技能清单（/ 菜单数据源）
        await get().loadCases()
        const s = get()
        if (s.currentCaseId) return
        // 恢复上次案件，否则取最近活跃案件
        let restore: string | null = null
        try {
          restore = localStorage.getItem(currentCaseKey())
        } catch {
          /* ignore */
        }
        const target = s.cases.find((c) => c.id === restore) ?? s.cases.find((c) => c.status === 'active') ?? null
        await get().setCurrentCase(target ? target.id : null)
      }
      // 启动竞态防护：Core 刚就绪（或崩溃自动重启）瞬间 RPC 可能失败，
      // 失败重试两次，避免重启后案件列表停留为空、历史记录失去挂载点
      for (let attempt = 1; ; attempt++) {
        try {
          await run()
          return
        } catch (e) {
          if (attempt >= 3) {
            get().toast(`初始化失败：${e instanceof Error ? e.message : String(e)}`)
            return
          }
          await sleep(1500 * attempt)
        }
      }
    },

    openWorkspacePicker: async () => {
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法切换工作空间')
        return
      }
      set({ openPop: null })
      // 后台会话里也可能有未终态任务（用户已切走的对话仍在跑）：同样不允许切换
      const busy =
        !!findRunningTaskMsg(get().messages) ||
        get().chatSessions.some((s) => !!findRunningTaskMsg(s.messages))
      if (busy) {
        get().toast('当前仍有任务在执行，请先停止再切换工作空间')
        return
      }
      if (get().wordEditingDirty) {
        get().toast('文档有未保存修改，请先保存并退出编辑后切换工作空间')
        return
      }
      let picked: string | null = null
      try {
        picked = await bridge.workspace.pickDir()
      } catch (e) {
        get().toast(`目录选择失败：${errorMessage(e)}`)
        return
      }
      if (!picked) return
      // Windows 路径大小写不敏感、分隔符可能不同：同一目录不做无谓的整体重载
      const norm = (p: string): string => p.replace(/[\\/]+$/, '').toLowerCase()
      if (norm(picked) === norm(get().space)) {
        get().toast('该目录已经是当前工作空间')
        return
      }
      await get().applyWorkspace(picked)
    },

    applyWorkspace: async (rootUri) => {
      const bridge = getBridge()
      if (!bridge) return
      // 切换前把当前对话落盘到旧工作空间的历史键下
      saveSession()
      try {
        const info = await bridge.workspace.open(rootUri)
        useWorkspaceHistory(info.rootUri)
        // 旧工作空间的案件/文档/产物/会话全部失效：整体重置后按新空间重新加载
        set({
          space: info.rootUri,
          openPop: null,
          chatSessions: loadHistory(),
          activeSessionId: null,
          conversationId: null,
          currentCaseId: null,
          cases: [],
          archivedCases: [],
          documents: [],
          docPages: {},
          docPageMeta: {},
          docAnnotations: {},
          curFileId: null,
          openTabs: [],
          rightView: 'list',
          citeTarget: null,
          messages: [],
          input: '',
          pendingAttachments: [],
          artifacts: [],
          artifactBodies: {},
          pendingClaims: [],
          staleClaims: [],
          conflictClaims: [],
          claimsQueueCount: 0,
          memories: [],
          contractOverviewId: null,
          docgenDraftId: null
        })
        try {
          localStorage.setItem(currentCaseKey(), '')
        } catch {
          /* ignore */
        }
        await get().loadModelCatalog()
        void get().loadCapabilities()
        await get().loadCases()
        const target = get().cases.find((c) => c.status === 'active') ?? null
        await get().setCurrentCase(target ? target.id : null)
        get().toast(`已切换工作空间：${info.rootUri}`)
      } catch (e) {
        get().toast(`切换失败：${errorMessage(e)}`)
      }
    },

    loadCases: async () => {
      const bridge = getBridge()
      if (!bridge) return
      const all = await bridge.case.list(true)
      set({
        cases: all.filter((c) => c.status !== 'archived'),
        archivedCases: all.filter((c) => c.status === 'archived')
      })
    },

    setCurrentCase: async (id) => {
      if (get().wordEditingDirty) {
        get().toast('文档有未保存修改，请先保存并退出编辑后切换任务')
        return
      }
      saveSession() // 切换案件前保存当前对话到历史
      const current = [...get().cases, ...get().archivedCases].find((c) => c.id === id)
      const modes: Record<string, ModeKey> = {
        legal_review: 'yj', contract_review: 'ht', document_generation: 'ws'
      }
      const mode = current ? modes[current.defaultMode] : get().mode
      // 切到另一模式的任务时按该模式记住的模型/思考强度恢复（首次进入继承当前选择）
      const selection = mode && mode !== get().mode ? modeSwitchPatch(mode) : {}
      set({
        currentCaseId: id,
        ...(mode ? { mode } : {}),
        ...selection,
        preset: null,
        contractStartOpen: false,
        contractOverviewId: null,
        bestPracticeTemplateId: null,
        docgenDocType: null,
        docgenStartOpen: false,
        pendingAttachments: [],
        pendingSkills: [],
        chatting: false,
        messages: [],
        input: '',
        openPop: null,
        conversationId: null,
        curFileId: null,
        openTabs: [],
        rightView: 'list',
        citeTarget: null,
        docPages: {},
        docPageMeta: {},
        docAnnotations: {},
        documents: [],
        artifacts: [],
        artifactBodies: {},
        docgenDraftId: null
      })
      try {
        localStorage.setItem(currentCaseKey(), id ?? '')
      } catch {
        /* ignore */
      }
      if (!id) return
      const bridge = getBridge()
      if (!bridge) return
      try {
        const authoritative = await bridge.case.get(id)
        if (get().currentCaseId !== id) return
        const lockedMode = modes[authoritative.defaultMode]
        if (!lockedMode) {
          get().toast('任务模式待确认，暂不可发起执行作业')
          return
        }
        set((s) => ({
          mode: lockedMode,
          // 案件列表可能滞后：以 Core 返回的模式为准再套一次该模式的模型/思考强度
          ...(lockedMode !== s.mode ? modeSwitchPatch(lockedMode) : {}),
          cases: [...s.cases.filter((c) => c.id !== id), ...(authoritative.status !== 'archived' ? [authoritative] : [])],
          archivedCases: [...s.archivedCases.filter((c) => c.id !== id), ...(authoritative.status === 'archived' ? [authoritative] : [])]
        }))
        void get().loadArtifacts()
        void get().loadClaimsQueue()
        const docs = await bridge.document.list(id)
        if (get().currentCaseId === id) set({ documents: docs.map(toDocMeta) })
      } catch (e) {
        get().toast(`文档加载失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    createCase: async (form) => {
      if (get().wordEditingDirty) {
        get().toast('请先保存当前文档，再新建任务')
        return false
      }
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法创建案件')
        return false
      }
      // UI 模式键 → Core 的 AgentMode（技术方案 §4.1）
      const modeMap = {
        yj: 'legal_review',
        ht: 'contract_review',
        ws: 'document_generation'
      } as const
      try {
        const c = await bridge.case.create({
          title: form.title,
          caseType: form.caseType,
          clientName: form.clientName,
          clientRole: form.clientRole || undefined,
          stage: form.stage || undefined,
          defaultMode: modeMap[form.defaultMode] ?? 'legal_review',
          clientRequestId: form.clientRequestId,
          linkedCaseId: form.defaultMode === 'ws' ? form.linkedCaseId || undefined : undefined,
          modeConfig: form.defaultMode === 'ht'
            ? { contractType: form.contractType ?? '', reviewBackground: form.reviewBackground ?? '' }
            : undefined,
          // 权限预设不再向用户暴露选择：模型外发是否脱敏由发起任务时用户决定；
          // 案件统一按受控外部建档（审计链路保持不变）
          permissionPreset: 'controlled_external'
        })
        await get().loadCases()
        await get().setCurrentCase(c.id)
        get().toast(`任务「${c.title}」已创建`)
        return true
      } catch (e) {
        // 创建失败保留表单内容（PRD 8.2）
        get().toast(`创建失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    archiveCase: async (id) => {
      if (get().currentCaseId === id && get().wordEditingDirty) {
        get().toast('请先保存当前文档，再归档任务')
        return
      }
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.case.archive(id)
        await get().loadCases()
        if (get().currentCaseId === id) {
          const next = get().cases.find((c) => c.status === 'active') ?? null
          await get().setCurrentCase(next ? next.id : null)
        }
        get().toast('案件已归档，处于只读状态')
      } catch (e) {
        get().toast(`归档失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    restoreCase: async (id) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.case.restore(id)
        await get().loadCases()
        const c = get().cases.find((x) => x.id === id)
        get().toast(`案件「${c?.title ?? ''}」已恢复`)
      } catch (e) {
        get().toast(`恢复失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    setDeletingCase: (c) => set({ deletingCase: c }),

    deleteCase: async (id) => {
      if (get().currentCaseId === id && get().wordEditingDirty) {
        get().toast('请先保存当前文档，再删除任务')
        return false
      }
      const bridge = getBridge()
      if (!bridge) return false
      try {
        const r = await bridge.case.delete({ caseId: id })
        get().toast(
          `任务「${r.title}」已永久删除（${r.documents} 份文档、${r.tasks} 个任务；审计记录保留）`
        )
        set({ deletingCase: null })
        await get().loadCases()
        if (get().currentCaseId === id) {
          const next = get().cases.find((c) => c.status === 'active') ?? null
          await get().setCurrentCase(next ? next.id : null)
        }
        return true
      } catch (e) {
        get().toast(`删除失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    // ---------------- 文档 ----------------

    importFiles: async (mode: 'file' | 'folder' = 'file') => {
      const bridge = getBridge()
      const { currentCaseId, toast } = get()
      if (!bridge) {
        toast('当前为浏览器调试模式，无法导入文件')
        return
      }
      if (!currentCaseId) {
        toast('请先选择或新建案件')
        return
      }
      const current = get().cases.find((c) => c.id === currentCaseId)
      if (current?.status === 'archived') {
        toast('案件已归档，处于只读状态；请先恢复案件')
        return
      }
      try {
        set({ importing: true, importProgress: null })
        const picked = await bridge.document.pick(mode)
        if (!picked) {
          set({ importing: false })
          return
        }
        // 文件夹内没有任何支持格式的文件（其余已自动过滤）
        if (!picked.token) {
          set({ importing: false })
          toast(
            picked.skipped > 0
              ? `文件夹内没有可导入的文件（已过滤 ${picked.skipped} 个不支持的文件）`
              : '所选文件夹为空'
          )
          return
        }
        if (picked.skipped > 0) {
          toast(`已自动过滤 ${picked.skipped} 个不支持的文件`)
        }
        // 分批导入期间逐批回报进度：立即刷新列表并更新「导入中 x/y」，
        // 用户不必等全部文件解析完成才看到结果
        const offProgress = bridge.document.onImportProgress((p) => {
          if (p.caseId !== currentCaseId) return
          set({ importProgress: { done: p.done, total: p.total, current: p.current } })
          void get().refreshDocuments()
        })
        let outcomes: ImportOutcome[] = []
        try {
          outcomes = await bridge.document.importByToken({
            caseId: currentCaseId,
            token: picked.token
          })
        } finally {
          offProgress()
        }
        const ok = outcomes.filter((o) => o.status === 'imported').length
        const dup = outcomes.filter((o) => o.status === 'duplicate').length
        const fail = outcomes.filter((o) => o.status === 'failed')
        const locked = outcomes.filter((o) => o.status === 'need_password')
        if (ok) toast(`已导入 ${ok} 个文件，原件只读保存`)
        if (dup) toast(`${dup} 个文件与本案件已有文件内容相同，已跳过`)
        for (const f of fail) toast(`导入失败：${f.name} —— ${f.reason ?? '未知原因'}`)
        if (locked.length) {
          toast(`${locked.length} 个文件已加密，需输入密码后使用`)
        }
        const docs = await bridge.document.list(currentCaseId)
        set({ documents: docs.map(toDocMeta) })
        // 加密文件：直接打开解锁窗口（REQ-DOC-004），仅首个待解锁文件
        const firstLocked = locked[0]
        if (firstLocked?.documentId) {
          set({
            unlockTarget: {
              documentId: firstLocked.documentId,
              title: firstLocked.title ?? firstLocked.name
            }
          })
        }
        // 图片与 PDF 文档自动启动 OCR（DEV-DOC-006：导入即可处理；PDF 含扫描页
        // 时页级 ocr_status=pending，无待识别页时 ocr.start 直接返回完成快照，无害）。
        // 并发发起：ocr.start 只登记后台任务并立即返回，串行 await 只会白等往返延迟。
        const ocrKinds = new Set(['png', 'jpg', 'tiff', 'pdf'])
        const ocrTargets = outcomes.filter(
          (o) => o.status === 'imported' && o.documentId && o.kind && ocrKinds.has(o.kind)
        )
        await Promise.all(
          ocrTargets.map((o) =>
            bridge.document.ocr.start(o.documentId!).catch(() => {
              /* OCR 启动失败不阻断导入结果展示 */
            })
          )
        )
        if (ocrTargets.length === 1) {
          toast(`已开始识别「${ocrTargets[0].name}」的文字（OCR）`)
        } else if (ocrTargets.length > 1) {
          toast(`已开始识别 ${ocrTargets.length} 个文件的文字（OCR）`)
        }
      } catch (e) {
        toast(`导入失败：${e instanceof Error ? e.message : String(e)}`)
      } finally {
        set({ importing: false, importProgress: null })
      }
    },

    // —— 输入坞随问附件（与案件文档导入独立：临时性、补充性，不进文档列表、不走 OCR） ——

    addAttachmentsFromPicker: async () => {
      const bridge = getBridge()
      const { model, toast, pendingAttachments } = get()
      if (!bridge) {
        toast('当前为浏览器调试模式，无法添加附件')
        return
      }
      const picked = await bridge.document.pickAttachments()
      if (!picked) return
      // 视觉门禁（Core 模型目录为唯一能力来源；目录未声明视为不支持）
      const supportsVision = findCatalogModel(get().modelCatalog, model)?.vision === true
      let blockedVision = 0
      const accepted: Array<ChatAttachment & { id: string }> = []
      for (const att of picked.attachments) {
        if (att.mime.startsWith('image/') && !supportsVision) {
          blockedVision++
          continue
        }
        accepted.push({ ...att, id: `att-${Date.now()}-${accepted.length}` })
      }
      const room = Math.max(0, 6 - pendingAttachments.length)
      const overflow = Math.max(0, accepted.length - room)
      const next = [...pendingAttachments, ...accepted.slice(0, room)]
      set({ pendingAttachments: next })
      if (blockedVision > 0) {
        toast(
          model
            ? `当前模型（${model}）不支持图片理解，已跳过 ${blockedVision} 个图片附件；可切换模型后再添加`
            : `尚未选择模型，已跳过 ${blockedVision} 个图片附件；请先在「模型设置」中添加支持图片理解的模型`
        )
      }
      if (overflow > 0) toast(`随问附件最多 6 个，已跳过 ${overflow} 个`)
      if (picked.skipped > 0) toast(`已跳过 ${picked.skipped} 个不支持的文件（正式材料请走案件文档导入）`)
    },

    addClipboardImageAttachment: async () => {
      const bridge = getBridge()
      const { model, toast } = get()
      if (!bridge) {
        toast('当前为浏览器调试模式，无法添加截图')
        return
      }
      if (findCatalogModel(get().modelCatalog, model)?.vision !== true) {
        toast(
          model
            ? `当前模型（${model}）不支持图片理解，暂不支持粘贴截图；可切换模型后再试`
            : '尚未选择模型，暂不支持粘贴截图；请先在「模型设置」中添加支持图片理解的模型'
        )
        return
      }
      if (get().pendingAttachments.length >= 6) {
        toast('随问附件最多 6 个，请先移除部分附件')
        return
      }
      const att = await bridge.document.readClipboardImage()
      if (!att) {
        toast('剪贴板中没有图片')
        return
      }
      set((s) => ({
        pendingAttachments: [...s.pendingAttachments, { ...att, id: `att-${Date.now()}` }]
      }))
    },

    removeAttachment: (id) =>
      set((s) => ({ pendingAttachments: s.pendingAttachments.filter((a) => a.id !== id) })),

    refreshDocuments: async () => {
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) return
      try {
        const docs = await bridge.document.list(caseId)
        set({ documents: docs.map(toDocMeta) })
      } catch {
        /* 轮询刷新失败静默，下次轮询重试 */
      }
    },

    ensurePages: async (docId) => {
      const cached = get().docPages[docId]
      if (cached) return cached
      const bridge = getBridge()
      if (!bridge) return []
      try {
        const pages: DocumentPage[] = await bridge.document.getPages(docId)
        const texts = pages.map((p) => p.text || '')
        set((s) => ({
          docPages: { ...s.docPages, [docId]: texts },
          docPageMeta: {
            ...s.docPageMeta,
            [docId]: pages.map((p) => ({
              pageNo: p.pageNo,
              quality: p.quality,
              corrected: !!p.corrected,
              ocrConfidence: p.ocrConfidence ?? null
            }))
          }
        }))
        return texts
      } catch (e) {
        get().toast(`读取文档失败：${e instanceof Error ? e.message : String(e)}`)
        return []
      }
    },

    invalidatePages: (docIds) => {
      if (docIds.length === 0) return
      set((s) => {
        const docPages = { ...s.docPages }
        const docPageMeta = { ...s.docPageMeta }
        for (const id of docIds) {
          delete docPages[id]
          delete docPageMeta[id]
        }
        return { docPages, docPageMeta }
      })
    },

    correctOcr: async (documentId, pageNo, correctedText, note) => {
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法更正文本')
        return 0
      }
      const cur = get().cases.find((c) => c.id === get().currentCaseId)
      if (cur?.status === 'archived') {
        get().toast('案件已归档，只读状态；请先恢复案件')
        return 0
      }
      try {
        const r = await bridge.document.correctOcr({ documentId, pageNo, correctedText, note })
        // 页文本已变更：清缓存并重取，阅读视图与检索结果即时更新
        set((s) => {
          const rest = { ...s.docPages }
          delete rest[documentId]
          return { docPages: rest }
        })
        await get().ensurePages(documentId)
        void get().loadClaimsQueue()
        get().toast(
          r.staleClaims > 0
            ? `第 ${pageNo} 页文本已更正；${r.staleClaims} 条引用已转待核实（原文位置变化）`
            : `第 ${pageNo} 页文本已更正（原件与页图不变）`
        )
        return r.staleClaims
      } catch (e) {
        get().toast(`更正失败：${e instanceof Error ? e.message : String(e)}`)
        return 0
      }
    },

    ensureAnnotations: async (docId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        // 产物（AI 生成文档）批注走 artifact 通道；映射为兼容形状存储
        const list = get().getRenderDoc(docId)?.isArtifact
          ? (await bridge.artifact.annotations(docId)).map(artifactAnnToDocAnn)
          : await bridge.document.annotations({ documentId: docId })
        set((s) => ({ docAnnotations: { ...s.docAnnotations, [docId]: list } }))
      } catch (e) {
        get().toast(`读取批注失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    addAnnotation: async (documentId, pageNo, body, quote, color) => {
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法添加批注')
        return false
      }
      const cur = get().cases.find((c) => c.id === get().currentCaseId)
      if (cur?.status === 'archived') {
        get().toast('案件已归档，只读状态；请先恢复案件')
        return false
      }
      try {
        if (get().getRenderDoc(documentId)?.isArtifact) {
          const ann = artifactAnnToDocAnn(
            await bridge.artifact.addAnnotation({ artifactId: documentId, body, quote, color })
          )
          set((s) => ({
            docAnnotations: {
              ...s.docAnnotations,
              [documentId]: [...(s.docAnnotations[documentId] ?? []), ann]
            }
          }))
          get().toast('批注已添加（产物原件不变）')
          return true
        }
        const ann = await bridge.document.addAnnotation({ documentId, pageNo, body, quote, color })
        set((s) => ({
          docAnnotations: {
            ...s.docAnnotations,
            [documentId]: [...(s.docAnnotations[documentId] ?? []), ann]
          }
        }))
        get().toast(`第 ${pageNo} 页批注已添加（原件不变）`)
        return true
      } catch (e) {
        get().toast(`添加批注失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    updateAnnotation: async (documentId, annotationId, body, color) => {
      const bridge = getBridge()
      if (!bridge) return false
      const cur = get().cases.find((c) => c.id === get().currentCaseId)
      if (cur?.status === 'archived') {
        get().toast('案件已归档，只读状态；请先恢复案件')
        return false
      }
      try {
        const isArtifact = get().getRenderDoc(documentId)?.isArtifact
        const ann = isArtifact
          ? artifactAnnToDocAnn(await bridge.artifact.updateAnnotation({ annotationId, body, color }))
          : await bridge.document.updateAnnotation({ annotationId, body, color })
        set((s) => ({
          docAnnotations: {
            ...s.docAnnotations,
            [documentId]: (s.docAnnotations[documentId] ?? []).map((a) => (a.id === ann.id ? ann : a))
          }
        }))
        return true
      } catch (e) {
        get().toast(`批注更新失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    setAnnotationResolved: async (documentId, annotationId, resolved) => {
      const bridge = getBridge()
      if (!bridge) return false
      const cur = get().cases.find((c) => c.id === get().currentCaseId)
      if (cur?.status === 'archived') {
        get().toast('案件已归档，只读状态；请先恢复案件')
        return false
      }
      try {
        const isArtifact = get().getRenderDoc(documentId)?.isArtifact
        const ann = isArtifact
          ? artifactAnnToDocAnn(await bridge.artifact.resolveAnnotation({ annotationId, resolved }))
          : await bridge.document.resolveAnnotation({ annotationId, resolved })
        set((s) => ({
          docAnnotations: {
            ...s.docAnnotations,
            [documentId]: (s.docAnnotations[documentId] ?? []).map((a) => (a.id === ann.id ? ann : a))
          }
        }))
        return true
      } catch (e) {
        get().toast(`批注状态更新失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    deleteAnnotation: async (documentId, annotationId) => {
      const bridge = getBridge()
      if (!bridge) return false
      const cur = get().cases.find((c) => c.id === get().currentCaseId)
      if (cur?.status === 'archived') {
        get().toast('案件已归档，只读状态；请先恢复案件')
        return false
      }
      try {
        if (get().getRenderDoc(documentId)?.isArtifact) {
          await bridge.artifact.deleteAnnotation({ annotationId })
        } else {
          await bridge.document.deleteAnnotation({ annotationId })
        }
        set((s) => ({
          docAnnotations: {
            ...s.docAnnotations,
            [documentId]: (s.docAnnotations[documentId] ?? []).filter((a) => a.id !== annotationId)
          }
        }))
        get().toast('批注已删除（审计保留删除记录）')
        return true
      } catch (e) {
        get().toast(`删除批注失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    getRenderDoc: (id) => {
      const d = get().documents.find((x) => x.id === id)
      if (d) {
        return {
          id: d.id,
          name: d.name,
          type: d.type,
          kind: d.type,
          date: d.date,
          pages: d.pages,
          caseId: d.caseId,
          pageTexts: get().docPages[d.id] ?? [],
          isRedacted: !!d.isRedacted
        }
      }
      // AI 产物（M2 收尾）：body 可能异步加载中（openDoc 预载，ReadingView 显示加载态）
      const a = get().artifacts.find((x) => x.id === id)
      if (a) {
        // 文件类产物（AI 生成的 Word/Excel/PPT 等）：kind 取真实扩展名，
        // 原件预览按真实格式渲染；md/结构化产物仍走 body 渲染
        const ext = a.title.includes('.') ? a.title.slice(a.title.lastIndexOf('.') + 1).toLowerCase() : ''
        const isFile = a.kind === 'file' && ext.length > 0
        return {
          id: a.id,
          name: a.title,
          type: isFile ? ext : 'md',
          kind: isFile ? ext : 'md',
          date: (a.createdAt || '').slice(0, 10),
          pages: 1,
          body: get().artifactBodies[a.id],
          isArtifact: true,
          isFileArtifact: isFile,
          docgenDraftId: a.docgenDraftId
        }
      }
      return undefined
    },

    loadArtifacts: async () => {
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) return
      try {
        set({ artifacts: await bridge.artifact.list(caseId) })
      } catch {
        /* 产物加载失败静默（概览弹窗显示空态） */
      }
    },

    ensureArtifactBody: async (artifactId) => {
      const bridge = getBridge()
      if (!bridge || get().artifactBodies[artifactId]) return
      try {
        const full = await bridge.artifact.get(artifactId)
        set((s) => ({ artifactBodies: { ...s.artifactBodies, [artifactId]: full.body } }))
      } catch (e) {
        get().toast(`产物加载失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    exportArtifact: async (artifactId, format, title) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const r = await bridge.artifact.export({ artifactId, format, title })
        if (!r) {
          get().toast('已取消导出')
          return
        }
        get().toast(`已导出 ${r.fileName}（${r.sizeBytes} 字节，写入审计）`)
      } catch (e) {
        get().toast(`导出失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    exportBundle: async (documentIds) => {
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) return null
      if (documentIds.length === 0) {
        get().toast('请先选择要装订的材料')
        return null
      }
      try {
        const r = await bridge.document.exportBundle({ caseId, documentIds })
        if (!r) {
          get().toast('已取消装订')
          return null
        }
        get().toast(
          `已装订 ${r.items.length} 份材料 / 共 ${r.pageCount} 页（${r.fileName}）`
        )
        if (r.failures.length > 0) {
          const detail = r.failures
            .slice(0, 3)
            .map((f) => `${f.title}：${f.reason}`)
            .join('；')
          get().toast(`有 ${r.failures.length} 份未装入——${detail}${r.failures.length > 3 ? '…' : ''}`)
        }
        return r
      } catch (e) {
        get().toast(`装订失败：${e instanceof Error ? e.message : String(e)}`)
        return null
      }
    },

    // ---------------- UI actions ----------------
    toast: (text) => {
      const id = toastSeq++
      set((s) => ({ toasts: [...s.toasts, { id, text }] }))
      setTimeout(() => {
        set((s) => ({ toasts: s.toasts.map((t) => (t.id === id ? { ...t, leaving: true } : t)) }))
        setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), 260)
      }, 2400)
    },

    setMode: (m) => {
      if (get().currentCaseId) {
        get().toast('当前任务模式已锁定，请新建任务选择其他模式')
        return
      }
      saveSession() // 切换模式前保存当前对话到历史（模式间对话隔离）
      const l = loadLayout(m)
      const nextRightView = l.rightView === 'docgen' && !get().docgenDraftId ? 'list' : l.rightView
      const selection = modeSwitchPatch(m)
      set({
        mode: m,
        ...selection,
        preset: null, // 模式切换清空预设（Agent 按用户意图自主选择）
        chatting: false,
        messages: [],
        input: '',
        openPop: null,
        conversationId: null,
        leftW: l.leftW,
        rightW: l.rightW,
        leftHide: l.leftHide,
        rightHide: l.rightHide,
        rightView: nextRightView
      })
    },

    setPreset: (p) => {
      set({ preset: p })
      const label = YJ_PRESETS.find((x) => x.key === p)?.name ?? p
      get().toast(`功能已切换：${label}`)
    },

    setInput: (v) => set({ input: v }),

    focusInput: () => set((s) => ({ inputFocusTick: s.inputFocusTick + 1 })),

    send: () => {
      const { input, mode, preset, currentCaseId, cases, toast } = get()
      const text = input.trim()
      // 文书起草：只选模板、不写文本也能发起（Core 生成内部任务描述，升级方案 §4.1）；
      // 其余模式仍要求输入问题或任务说明
      if (!text && !(mode === 'ws' && !get().docgenDraftId)) return
      if (!currentCaseId) {
        toast('请先选择或新建案件')
        return
      }
      const current = cases.find((c) => c.id === currentCaseId)
      const expected = { yj: 'legal_review', ht: 'contract_review', ws: 'document_generation' }[mode]
      if (!current || current.defaultMode !== expected) {
        toast('任务模式待确认或不匹配，无法发起执行作业')
        return
      }
      if (current?.status === 'archived') {
        toast('案件已归档，只读状态；请先恢复案件再发起任务')
        return
      }
      // 策略预设（REQ-STR-001 放宽）：不再强制预先设置委托人身份。
      // 输入明确给出立场（如「为被告生成一份辩护策略」）直接随任务发起；
      // 未明确时先弹立场确认框，用户选择后再发起（保留输入文本，走 startStrategyTask）
      if (mode === 'yj' && preset === 'strategy') {
        const stance = detectStance(text)
        if (stance) {
          get().runReviewTask(text, preset, stance)
          return
        }
        set({ strategyStartOpen: true })
        return
      }
      // 合同审查：先在发起对话框确认主合同 / 审查方 / 风险偏好（PRD 14.2）；保留输入文本
      if (mode === 'ht') {
        if (get().contractOverviewId) get().runReviewTask(text, null)
        else toast('请先完成合同审查概览，再进入对话审查')
        return
      }
      // 文书生成：已打开草稿时底部输入即 AI 修订；未打开草稿则发起新的起草任务
      if (mode === 'ws') {
        const draftId = get().docgenDraftId
        if (draftId) {
          void get().startDocRevision(draftId, text)
          return
        }
        get().runReviewTask(text, null)
        return
      }
      // 阅卷：直接发起任务（自主 Agent Loop：模型自主决策工具调用与流程）
      get().runReviewTask(text, preset)
    },

    runReviewTask: (text, preset, stance) => {
      const { currentCaseId, documents, model, thinking, pendingSkills, skills, toast, mode } = get()
      if (!currentCaseId) return
      // 模型目录由用户维护：一个模型都没有时直接引导去添加，不发一次必然失败的请求
      if (!model) {
        get().setApiKeyDialogOpen(true)
        toast('尚未添加模型：请在「模型设置」中填写模型 ID、API 密钥并测试连通后再发起任务')
        return
      }
      const conversationId = get().conversationId
      const bestPracticeTemplateId = get().bestPracticeTemplateId
      const docgenDocType = get().docgenDocType
      const contractOverviewId = get().contractOverviewId

      // 跨任务对话历史：最近几轮 user/assistant 文本（技能安装等流程续接上下文）
      const history: ChatHistoryTurn[] = get()
        .messages.slice(-12)
        .map((m): ChatHistoryTurn | null => {
          if (m.role === 'user') return { role: 'user', text: m.text }
          if (m.role === 'task') {
            const text =
              (m.chat ?? [])
                .filter((c) => c.kind === 'assistant' && c.text)
                .map((c) => c.text ?? '')
                .join('\n') || (m.streamText ?? '')
            return text.trim() ? { role: 'assistant', text } : null
          }
          return null
        })
        .filter((h): h is ChatHistoryTurn => h !== null)
        .slice(-10)

      const uid = newMsgId()
      // 文书起草允许空文本：气泡展示本轮选定的模板，实际任务文本由 Core 按模板生成
      // 内部描述（不在输入框里堆一大段预填文本，升级方案 §4.1）
      const docType = get().docgenDocType
      const bubbleText = text || (docType ? `按所选模板起草《${docType}》` : '按所选模板起草法律文书')
      // 本轮显式选定的技能名随 user 气泡留存（发送后 pending* 清空，气泡是唯一回看处）
      const skillNames = pendingSkills.map((id) => skills.find((s) => s.id === id)?.name ?? id)
      // 随问附件随本轮任务直发模型（不导入案件文档；发送后清空，气泡留附件名回看）
      const attachmentNames = get().pendingAttachments.map((a) => a.name)
      const attachments = get().pendingAttachments.map(({ id: _id, ...att }) => att)
      // 显式选择的预设只作用于当前这条输入，发送后清空回到自主推断
      set((s) => ({
        chatting: true,
        preset: null,
        input: '',
        openPop: null,
        pendingAttachments: [],
        messages: [
          ...s.messages,
          {
            id: uid,
            role: 'user',
            text: bubbleText,
            createdAt: Date.now(),
            skillNames: skillNames.length ? skillNames : undefined,
            attachmentNames: attachmentNames.length ? attachmentNames : undefined
          }
        ]
      }))

      const bridge = getBridge()
      if (!bridge) {
        // 浏览器调试模式：模拟任务完成态
        const tid = newMsgId()
        set((s) => ({
          messages: [
            ...s.messages,
            { id: tid, role: 'task' as const, taskId: `demo-${tid}`, lifecycle: 'RUNNING', steps: [{ label: '检索案件材料', state: 'active' }] }
          ]
        }))
        setTimeout(() => {
          const body = makeAiReply('yj', documents)
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === tid && m.role === 'task'
                ? { ...m, lifecycle: 'COMPLETED', steps: m.steps.map((x) => ({ ...x, state: 'done' as const })), body }
                : m
            )
          }))
          upsertSession(false) // 演示回复完成同样增量落盘，历史记录实时可见
        }, 1100)
        return
      }

      void (async () => {
        try {
          const task = await bridge.agent.start({
            caseId: currentCaseId,
            mode: mode === 'ws' ? 'document_generation' : mode === 'ht' ? 'contract_review' : 'legal_review',
            workflowKind: mode === 'ws' ? 'docgen_dialogue' : mode === 'ht' ? 'contract_dialogue' : undefined,
            docgenInput: mode === 'ws' ? {
              templateId: bestPracticeTemplateId ?? undefined,
              docType: docgenDocType ?? undefined,
              // 自然语言任务即起草需求；Core 在模型未建草稿时据此兜底产出 Word
              purpose: text.slice(0, 4000)
            } : undefined,
            contractInput: mode === 'ht' && contractOverviewId ? { overviewId: contractOverviewId } : undefined,
            input: text,
            workflowPreset: preset ?? undefined,
            // 辩护策略立场（本次任务生效，不回写案件设置）：输入明确或对话框确认后传入
            strategyStance: preset === 'strategy' && stance ? stance : undefined,
            modelPolicy: { modelId: model, remote: true, reasoningEffort: thinking },
            // / 菜单选中项随任务发送（技能绑定），发送后清空
            skills: pendingSkills.length ? [...pendingSkills] : undefined,
            // 随问附件直发模型（临时性补充材料，不导入案件文档）
            attachments: attachments.length ? attachments : undefined,
            // 最近几轮对话文本：无会话时的兜底（有 conversationId 时 Core 用全文历史覆盖）
            history: history.length ? history : undefined,
            // 同会话续聊（V26）：Core 持久化跨任务全文历史
            conversationId: conversationId ?? undefined
          })
          if (get().currentCaseId !== currentCaseId || get().conversationId !== conversationId) return
          set({ pendingSkills: [], conversationId: task.conversationId ?? null })
          const tid = newMsgId()
          set((s) => ({
            messages: [
              ...s.messages,
              { id: tid, role: 'task', taskId: task.id, lifecycle: task.lifecycle, steps: [], chat: [], startedAt: Date.now() }
            ]
          }))
          await startTaskFeed(tid, task.id)
        } catch (e) {
          toast(`任务发起失败：${e instanceof Error ? e.message : String(e)}`)
        }
      })()
    },

    restart: () => {
      // 开新对话：未完成任务不取消 —— 会话（含任务生命周期与事件游标）落盘后，
      // 订阅保持活跃，事件经后台路径续写会话快照；回到该会话即可续看进度
      saveSession()
      set({
        chatting: false,
        messages: [],
        input: '',
        openPop: null,
        conversationId: null,
        // 新对话重新选择模板，避免上一轮模板影响自然语言起草
        bestPracticeTemplateId: null,
        docgenDocType: null
      })
    },

    openSession: async (id) => {
      const target = get().chatSessions.find((x) => x.id === id)
      if (!target) return
      // 会话属于其他案件：先切案件（内部会保存当前对话），否则原地保存当前对话
      if (target.caseId && target.caseId !== get().currentCaseId) {
        await get().setCurrentCase(target.caseId)
      } else {
        saveSession()
      }
      // 保存后再查找目标会话，避免读到保存前的旧快照（如正在续聊的同一条会话）
      const sess = get().chatSessions.find((x) => x.id === id)
      if (!sess) return
      set({
        messages: snapshotMessages(sess.messages),
        chatting: false,
        input: '',
        openPop: null,
        activeSessionId: id,
        conversationId: sess.conversationId ?? null
      })
      // 未终态任务重挂事件订阅（已在订阅中则幂等跳过）；存量事件按 lastSeq 增量补齐。
      // 挂起等回答的任务没有新事件，单独补拉待答问题集以恢复问题卡
      for (const m of sess.messages) {
        if (m.role !== 'task' || TERMINAL.includes(m.lifecycle)) continue
        if (m.lifecycle === 'WAITING_HUMAN' && !m.questionSet) {
          void get().loadPendingQuestions(m.id, m.taskId)
        } else {
          void startTaskFeed(m.id, m.taskId)
        }
      }
    },

    deleteSession: (id) => {
      const target = get().chatSessions.find((x) => x.id === id)
      const sessions = get().chatSessions.filter((x) => x.id !== id)
      const deletingCurrent = get().activeSessionId === id
      // 删除的是当前打开的会话：整体回到新对话状态（清空消息与 Core 会话锚点），
      // 否则视图仍持有旧消息，下次发言会经 upsertSession 原样重建——「删了个寂寞」
      set((s) => ({
        chatSessions: sessions,
        activeSessionId: s.activeSessionId === id ? null : s.activeSessionId,
        ...(deletingCurrent ? { messages: [], conversationId: null, chatting: false, input: '', openPop: null } : {})
      }))
      saveHistory(sessions)
      // 被删会话内未终态任务（含后台执行中）一并取消：删除后事件无处落，任务不能成为不可停的孤儿
      const bridge = getBridge()
      const curMsgIds = new Set(get().messages.map((m) => m.id))
      for (const m of target?.messages ?? []) {
        if (m.role !== 'task' || TERMINAL.includes(m.lifecycle)) continue
        if (!deletingCurrent && curMsgIds.has(m.id)) {
          void get().stopTask(m.id, m.taskId) // 当前对话可见：走停止按钮同路径（UI 置取消中）
        } else {
          endFeed(m.taskId)
          if (bridge) void bridge.agent.cancel(m.taskId).catch(() => undefined)
        }
      }
      get().toast('已删除该会话')
    },

    attachPreviewUrl: (callId, url) => {
      // 只回写一次：卡片重复挂载（全屏 Portal 等）不会反复触发 set；
      // 同时更新后台会话快照——任务在后台完成时消息不在当前对话里
      const patch = (chat?: TaskChatItem[]): TaskChatItem[] | undefined =>
        chat?.map((c) =>
          c.callId === callId && c.kind === 'tool' && !c.previewUrl ? { ...c, previewUrl: url } : c
        )
      set((s) => ({
        messages: s.messages.map((m) =>
          m.role === 'task' && m.chat?.some((c) => c.callId === callId) ? { ...m, chat: patch(m.chat) } : m
        ),
        chatSessions: s.chatSessions.map((sess) => ({
          ...sess,
          messages: sess.messages.map((m) =>
            m.role === 'task' && m.chat?.some((c) => c.callId === callId) ? { ...m, chat: patch(m.chat) } : m
          )
        }))
      }))
    },

    toggleRecording: () => {
      const on = !get().recording
      set({ recording: on })
      if (on) get().toast('语音输入已开启（演示）')
    },

    setModel: (m) => {
      // 思考强度按模型单独记忆：切回某个模型时恢复它上次选的档位；
      // 该模型还没选过则用它自己的默认档位（目录声明的 reasoningDefault），
      // 都不支持档位调节时归零为 off（界面显示「不支持档位调节」）
      const model = findCatalogModel(get().modelCatalog, m)
      const levels = (model?.reasoningLevels ?? []) as Exclude<ReasoningEffort, 'off'>[]
      const remembered = get().thinkingByModel[m]
      const rememberedOk =
        remembered !== undefined && (remembered === 'off' || levels.includes(remembered))
      const fallback = ((model?.reasoningDefault || levels[levels.length - 1] || 'off') as ReasoningEffort)
      const next: ReasoningEffort = levels.length ? (rememberedOk ? remembered : fallback) : 'off'
      set((s) => ({
        model: m,
        thinking: next,
        openPop: null,
        // 只更新当前模式的选择记录：另一模式（如法律阅卷）的模型偏好不被覆盖
        modeSelection: { ...s.modeSelection, [s.mode]: { model: m, thinking: next } }
      }))
      if (!levels.length) {
        get().toast(`已切换模型：${m}（该模型未声明思考档位，可在「模型设置」中为该模型添加档位）`)
      } else {
        get().toast(`已切换模型：${m} · 思考强度 ${THINKING_LABEL[next]}`)
      }
      // 模型归属服务商随目录确定：切换模型即同步「当前服务商」，保证界面显示的
      // 「API Key 已配置」与实际发请求使用的密钥一致
      const owner = providerOfModel(get().modelCatalog, m)
      if (owner && owner !== get().modelProvider) void get().setModelProvider(owner)
    },

    setThinking: (v) => {
      const modelId = get().model
      set((s) => ({
        thinking: v,
        thinkingByModel: modelId ? { ...s.thinkingByModel, [modelId]: v } : s.thinkingByModel,
        modeSelection: { ...s.modeSelection, [s.mode]: { model: s.model, thinking: v } }
      }))
      get().toast(`思考强度：${THINKING_LABEL[v]}`)
    },

    loadCapabilities: async () => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const skills = await bridge.skill.list()
        set({ skills })
      } catch {
        /* / 菜单数据加载失败不阻断输入；下次打开重试 */
      }
    },

    togglePendingSkill: (id) => {
      set((s) => ({
        pendingSkills: s.pendingSkills.includes(id) ? s.pendingSkills.filter((x) => x !== id) : [...s.pendingSkills, id]
      }))
    },

    setOpenPop: (p) => set({ openPop: p }),
    setCasePanelOpen: (v) => set({ casePanelOpen: v }),
    setCreatingCase: (v) => set({ creatingCase: v }),

    selectFile: (id) => {
      const { openTabs } = get()
      const doc = get().getRenderDoc(id)
      if (!doc) return
      // 真实卷宗懒加载页文本；AI 产物懒加载结构化 body
      if (doc.pageTexts) void get().ensurePages(id)
      if (doc.isArtifact) void get().ensureArtifactBody(id)
      set({
        curFileId: id,
        openTabs: openTabs.includes(id) ? openTabs : [...openTabs, id],
        rightView: 'read',
        rightHide: false, // 右栏收起时点击文件卡/列表项：阅读窗口自动弹出
        docSearchOpen: false,
        docSearchTerm: '',
        citeTarget: null
      })
      document.documentElement.style.setProperty('--zoom', '1')
      set({ zoom: 1 })
      persist()
    },

    openDoc: (id) => {
      if (get().wordEditingDirty) {
        get().toast('请先保存当前文档再打开其他文档')
        return
      }
      get().selectFile(id)
    },

    closeTab: (id) => {
      const { openTabs, curFileId } = get()
      const idx = openTabs.indexOf(id)
      if (idx < 0) return
      const next = openTabs.filter((x) => x !== id)
      set({ openTabs: next })
      if (curFileId === id) {
        if (next.length) {
          get().selectFile(next[Math.min(idx, next.length - 1)])
        } else {
          set({ curFileId: null, rightView: 'list', fullscreen: false, citeTarget: null })
          persist()
        }
      }
    },

    showList: () => {
      set({ rightView: 'list' })
      persist()
    },

    backToList: () => {
      // 返回列表：退出全屏、关闭预览与产物弹窗、保留已打开标签（项目约定）
      set({ rightView: 'list', fullscreen: false, artOpen: false, citeTarget: null })
      persist()
    },

    setZoom: (z) => {
      const zoom = Math.min(1.6, Math.max(0.6, Math.round(z * 10) / 10))
      document.documentElement.style.setProperty('--zoom', String(zoom))
      set({ zoom })
    },

    setPreviewMode: (m) => set({ previewMode: m }),

    setFullscreen: (v) => set({ fullscreen: v }),

    setDocSearch: (open, term) => set((s) => ({ docSearchOpen: open, docSearchTerm: term ?? s.docSearchTerm })),

    openCitation: (docId, pageNo) => {
      if (get().wordEditingDirty) { get().toast('请先保存当前文档再定位原文'); return }
      get().openDoc(docId)
      set((s) => ({ citeTarget: { docId, pageNo, nonce: (s.citeTarget?.nonce ?? 0) + 1 } }))
      get().toast('已定位至原文位置')
    },

    openCitationTarget: async (target) => {
      if (get().wordEditingDirty) { get().toast('请先保存当前文档再定位原文'); return }
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) return
      const docs = await bridge.document.list(caseId)
      if (get().currentCaseId !== caseId) return
      const doc = docs.find((d) => d.id === target.documentId)
      if (!doc || (target.documentVersionId && doc.activeVersionId !== target.documentVersionId)) {
        get().toast('引证来源不存在或版本已失效，请重新核验')
        return
      }
      get().openDoc(target.documentId)
      // 原文定位优先回到原件视图（PDF 走文本层），不再强制切到提取文本模式
      set((s) => ({ previewMode: supportsOriginalPreview(doc.kind) ? 'render' : 'text',
        citeTarget: { docId: target.documentId, pageNo: target.pageNo, quote: target.quote,
          documentVersionId: target.documentVersionId, nonce: (s.citeTarget?.nonce ?? 0) + 1 } }))
    },

    toggleArt: () => set((s) => ({ artOpen: !s.artOpen })),

    closeArt: (force) => {
      if (force || !get().artPinned) set({ artOpen: false })
    },

    toggleArtPin: () => set((s) => ({ artPinned: !s.artPinned })),

    toggleArtExpanded: () => set((s) => ({ artExpanded: !s.artExpanded })),

    setAuditOpen: (v) => set({ auditOpen: v }),

    setClaimsQueueOpen: (v) => set({ claimsQueueOpen: v }),

    loadClaimsQueue: async () => {
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) return
      set({ claimsLoading: true })
      try {
        const [pending, stale, conflict] = await Promise.all([
          bridge.claim.list({ caseId, status: 'needs_human' }),
          bridge.claim.list({ caseId, status: 'stale' }),
          bridge.claim.list({ caseId, status: 'conflict' })
        ])
        set({
          pendingClaims: pending,
          staleClaims: stale,
          conflictClaims: conflict,
          claimsQueueCount: pending.length + stale.length + conflict.length,
          claimsLoading: false
        })
      } catch (e) {
        set({ claimsLoading: false })
        get().toast(`待核实队列加载失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    openClaimsQueue: () => {
      set({ claimsQueueOpen: true })
    },

    verifyClaim: async (claimId, decision, note) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.claim.verify({ claimId, decision, note })
        get().toast(decision === 'confirmed' ? '已标记为用户确认（写入审计）' : '已驳回并移除（写入审计）')
        await get().loadClaimsQueue()
      } catch (e) {
        get().toast(`核实失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    recheckClaim: async (claimId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const r = await bridge.claim.recheck(claimId)
        get().toast(r.recovered ? '重新校验通过，已恢复为已核实' : `无法恢复：${r.reasonCode ?? '来源或引语已变化'}`)
        await get().loadClaimsQueue()
      } catch (e) {
        get().toast(`重验失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    resolveConflict: async (claimId, decision, note) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.claim.resolveConflict({ claimId, decision, note })
        get().toast(decision === 'confirmed' ? '已采纳该侧（对侧转待核实，写入审计）' : '矛盾双方已整体驳回（写入审计）')
        await get().loadClaimsQueue()
      } catch (e) {
        get().toast(`矛盾裁决失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    setMemoryOpen: (v) => set({ memoryOpen: v }),

    loadMemories: async () => {
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) return
      set({ memoryLoading: true })
      try {
        const memories = await bridge.memory.list({ caseId, includeShared: true })
        set({ memories, memoryLoading: false })
      } catch (e) {
        set({ memoryLoading: false })
        get().toast(`记忆加载失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    correctMemory: async (memoryId, value, note) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.memory.correct({ memoryId, value, note })
        get().toast('已更正：新值生效，原值保留可追溯（写入审计）')
        await get().loadMemories()
      } catch (e) {
        get().toast(`更正失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    forgetMemory: async (memoryId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.memory.forget({ memoryId })
        get().toast('记忆已删除，不再参与召回与上下文装配')
        await get().loadMemories()
      } catch (e) {
        get().toast(`删除失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    deleteDocuments: async (docIds) => {
      const bridge = getBridge()
      if (!bridge) return
      let ok = 0
      let failed = 0
      for (const id of docIds) {
        try {
          await bridge.document.delete(id)
          ok += 1
          // 逐份关闭被删文档的标签
          if (get().openTabs.includes(id)) get().closeTab(id)
        } catch {
          failed += 1 // 单份失败不中断批量流程
        }
      }
      get().toast(failed ? `已删除 ${ok} 份，${failed} 份失败（可重试）` : `已删除 ${ok} 份文档`)
      await get().refreshDocuments()
      if (ok > 0) {
        // 失效 Claim / 记忆计数即时更新（侧栏角标联动）
        void get().loadClaimsQueue()
        void get().loadMemories()
      }
    },

    deleteDocument: async (docId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const r = await bridge.document.delete(docId)
        get().toast(
          r.staleClaims > 0 || r.staleMemories > 0
            ? `文档已删除；${r.staleClaims} 项结论、${r.staleMemories} 条记忆已标记失效`
            : '文档已删除'
        )
        await get().refreshDocuments()
        // 关闭被删文档的标签
        const s = get()
        if (s.openTabs.includes(docId)) {
          s.closeTab(docId)
        }
        // 失效 Claim 计数即时更新（侧栏角标联动）
        void get().loadClaimsQueue()
        void get().loadMemories()
      } catch (e) {
        get().toast(`删除失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    setApiKeyDialogOpen: (v) => set({ apiKeyDialogOpen: v }),

    loadModelCatalog: async () => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const catalog = await bridge.model.catalog()
        // 当前模型不在任何服务商目录里（旧版本存过已下线模型 / 目录被改过）时
        // 回落到当前（或默认）服务商的第一个模型；用户还没添加任何模型时
        // 保持空串，避免输入坞显示一个并不存在的模型名
        const current = get().model
        const fallbackProvider =
          catalog.providers.find((p) => p.id === catalog.defaultProvider) ?? catalog.providers[0] ?? null
        const known = catalog.providers.some((p) => p.models.some((m) => m.id === current))
        const fallbackModel = fallbackProvider?.models[0]?.id ?? ''
        const modelProvider = catalog.providers.some((p) => p.id === get().modelProvider)
          ? get().modelProvider
          : (fallbackProvider?.id ?? 'zhipu')
        const activeProvider = known ? (providerOfModel(catalog, current) ?? modelProvider) : modelProvider
        const active = catalog.providers.find((p) => p.id === activeProvider)
        // 当前模型的思考强度：优先用该模型上次选过的档位，否则用目录声明的默认档位；
        // 未声明档位的模型一律置 off（输入坞显示「无档位」而不是一个不生效的值）
        const activeModelId = known ? current : fallbackModel
        const activeModel = findCatalogModel(catalog, activeModelId)
        const activeLevels = (activeModel?.reasoningLevels ?? []) as Exclude<ReasoningEffort, 'off'>[]
        const remembered = get().thinkingByModel[activeModelId]
        const rememberedOk =
          remembered !== undefined && (remembered === 'off' || activeLevels.includes(remembered))
        const nextThinking: ReasoningEffort = activeLevels.length
          ? rememberedOk
            ? remembered
            : ((activeModel?.reasoningDefault || activeLevels[activeLevels.length - 1]) as ReasoningEffort)
          : 'off'
        set((s) => ({
          modelCatalog: catalog,
          modelProvider,
          ...(known ? {} : { model: fallbackModel }),
          thinking: nextThinking,
          // 目录刷新后当前模式的选择同步收敛（模型被下线时落到可用模型）；
          // 一个模型都没有时不记录，避免把空模型固化成该模式的选择
          modeSelection: activeModelId
            ? { ...s.modeSelection, [s.mode]: { model: activeModelId, thinking: nextThinking } }
            : s.modeSelection,
          apiKeyConfigured: !!active?.configured
        }))
      } catch {
        /* 目录加载失败不阻断开机：能力门禁按 fail-closed 处理，下次重试 */
      }
    },

    saveModelProvider: async (req) => {
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法保存模型服务商')
        return false
      }
      try {
        await bridge.model.saveProvider(req)
        await get().loadModelCatalog()
        // 保存后不关闭对话框：调用方紧接着会跑一次连通测试并把结果显示在当前面板，
        // 成功提示由调用方给出（避免一次保存弹两条 toast）
        return true
      } catch (e) {
        get().toast(`保存失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    removeModelProvider: async (providerId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.model.removeProvider({ providerId })
        await get().loadModelCatalog()
        get().toast(`已删除服务商：${providerId}`)
      } catch (e) {
        get().toast(`删除失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    resetModelProvider: async (providerId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const r = await bridge.model.resetProvider({ providerId })
        await get().loadModelCatalog()
        get().toast(r.reset ? '已清空自定义模型目录（内置预设不再带模型，请重新添加）' : '当前已是官方默认配置')
      } catch (e) {
        get().toast(`恢复失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    setApiKey: async (value, providerId) => {
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法保存 API Key')
        return
      }
      const provider = providerId ?? get().modelProvider
      try {
        await bridge.settings.set({ key: `model.${provider}ApiKey`, value })
        await get().loadModelCatalog()
        get().toast(value.trim() === '' ? '已清除模型 API Key' : '模型 API Key 已保存（仅存本机）')
      } catch (e) {
        get().toast(`保存失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    setModelProvider: async (v) => {
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法切换服务商')
        return
      }
      try {
        await bridge.model.setProvider({ providerId: v })
        // 当前模型不在新服务商目录里时，切到该服务商的第一个模型，
        // 否则界面显示的 Key 状态与实际发请求的端点不一致；
        // 该服务商还没添加任何模型时置空，由界面引导用户去添加
        const conf = get().modelCatalog?.providers.find((p) => p.id === v)
        const prevModel = get().model
        const keep = conf?.models.some((m) => m.id === prevModel) ?? true
        const nextModel = keep ? prevModel : (conf?.models[0]?.id ?? '')
        // 思考强度同样按模型记忆：换到别的模型时切换到它自己的档位
        const nextConf = findCatalogModel(get().modelCatalog, nextModel)
        const nextLevels = (nextConf?.reasoningLevels ?? []) as Exclude<ReasoningEffort, 'off'>[]
        const remembered = get().thinkingByModel[nextModel]
        const rememberedOk =
          remembered !== undefined && (remembered === 'off' || nextLevels.includes(remembered))
        const nextThinking: ReasoningEffort = nextLevels.length
          ? rememberedOk
            ? remembered
            : ((nextConf?.reasoningDefault || nextLevels[nextLevels.length - 1]) as ReasoningEffort)
          : 'off'
        set((s) => ({
          modelProvider: v,
          model: nextModel,
          thinking: nextThinking,
          // 服务商切换只影响当前模式的选择记录
          modeSelection: { ...s.modeSelection, [s.mode]: { model: nextModel, thinking: nextThinking } },
          apiKeyConfigured: !!conf?.configured
        }))
        if (!nextModel) {
          get().toast(`已切换到 ${conf?.label ?? v}，该服务商尚未添加模型，请在「模型设置」中添加`)
        } else if (nextModel !== prevModel) {
          get().toast(`已切换到 ${conf?.label ?? v}，模型随之改为 ${nextModel}`)
        }
      } catch (e) {
        get().toast(`切换失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    openUnlock: (documentId, title) => set({ unlockTarget: { documentId, title } }),

    closeUnlock: () => set({ unlockTarget: null }),

    unlockDocument: async (password) => {
      const bridge = getBridge()
      const target = get().unlockTarget
      if (!bridge || !target) return false
      try {
        const result = await bridge.document.requestUnlock({
          documentId: target.documentId,
          password
        })
        set({ unlockTarget: null })
        get().toast(`「${target.title}」已解锁，共 ${result.pageCount} 页`)
        const caseId = get().currentCaseId
        if (caseId) {
          const docs = await bridge.document.list(caseId)
          set({ documents: docs.map(toDocMeta) })
        }
        return true
      } catch (e) {
        // 密码错误 / 不支持加密：原因由 Core 给出，密码本身不进入日志
        get().toast(`解锁失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    setRedactionDialogOpen: (v) => set({ redactionDialogOpen: v }),

    setContractStartOpen: (v) => set({ contractStartOpen: v }),

    startContractTask: (text, inp) => {
      // 表单确认后直接发起任务（自主 Agent Loop：流程由模型自主决策）
      set({ contractStartOpen: false })
      void get().runContractTask(text, inp)
    },

    setStrategyStartOpen: (v) => set({ strategyStartOpen: v }),

    startStrategyTask: (stance) => {
      // 立场确认后发起（输入文本保留在 store.input，此处取用后由 runReviewTask 清空）
      const text = get().input.trim()
      set({ strategyStartOpen: false })
      if (!text) return
      void get().runReviewTask(text, 'strategy', stance)
    },

    runContractTask: async (text, inp) => {
      const { currentCaseId, model, thinking, toast } = get()
      if (!currentCaseId) return
      const bridge = getBridge()
      if (!bridge) {
        toast('浏览器调试模式不支持合同审查')
        return
      }
      const uid = newMsgId()
      // 合同审查不支持随问附件：有遗留附件时提示并清空（不静默丢弃）
      if (get().pendingAttachments.length) {
        toast('随问附件仅在阅卷问答中生效，已清空本次附件')
        set({ pendingAttachments: [] })
      }
      set((s) => ({ chatting: true, input: '', contractStartOpen: false, messages: [...s.messages, { id: uid, role: 'user', text, createdAt: Date.now() }] }))
      try {
        const task = await bridge.agent.start({
          caseId: currentCaseId,
          mode: 'contract_review',
          input: text,
          contractInput: inp,
          modelPolicy: { modelId: model, remote: true, reasoningEffort: thinking },
          // 同会话续聊（V26）：Core 持久化跨任务全文历史
          conversationId: get().conversationId ?? undefined
        })
        set({ conversationId: task.conversationId ?? null })
        const tid = newMsgId()
        set((s) => ({
          messages: [
            ...s.messages,
            { id: tid, role: 'task' as const, taskId: task.id, lifecycle: task.lifecycle, steps: [], chat: [], startedAt: Date.now() }
          ]
        }))
        await startTaskFeed(tid, task.id)
      } catch (e) {
        toast(`任务发起失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    setDocgenStartOpen: (v) => set({ docgenStartOpen: v }),

    startDocGenTask: async (text, inp) => {
      const { currentCaseId, model, thinking, toast } = get()
      if (!currentCaseId) return { ok: false, error: '请先选择或新建案件' }
      const bridge = getBridge()
      if (!bridge) {
        return { ok: false, error: '当前为浏览器调试模式，无法发起文书生成' }
      }
      const uid = newMsgId()
      // 文书生成不支持随问附件：有遗留附件时提示并清空（不静默丢弃）
      if (get().pendingAttachments.length) {
        toast('随问附件仅在阅卷问答中生效，已清空本次附件')
        set({ pendingAttachments: [] })
      }
      // 对话框保持挂载：发起成功才关闭（表单状态不丢）；失败时移除占位消息并交回错误
      set((s) => ({ chatting: true, input: '', messages: [...s.messages, { id: uid, role: 'user', text, createdAt: Date.now() }] }))
      try {
        const task = await bridge.agent.start({
          caseId: currentCaseId,
          mode: 'document_generation',
          input: text,
          docgenInput: inp,
          modelPolicy: { modelId: model, remote: true, reasoningEffort: thinking },
          // 同会话续聊（V26）：Core 持久化跨任务全文历史
          conversationId: get().conversationId ?? undefined
        })
        set({ conversationId: task.conversationId ?? null })
        const tid = newMsgId()
        set((s) => ({
          docgenStartOpen: false,
          messages: [
            ...s.messages,
            { id: tid, role: 'task' as const, taskId: task.id, lifecycle: task.lifecycle, steps: [], chat: [], startedAt: Date.now() }
          ]
        }))
        await startTaskFeed(tid, task.id)
        return { ok: true }
      } catch (e) {
        // 发起失败保留对话框与表单内容，错误交给对话框展示（类型不匹配可确认后重发）
        set((s) => ({ chatting: false, messages: s.messages.filter((m) => m.id !== uid) }))
        return { ok: false, error: e instanceof Error ? e.message : String(e) }
      }
    },

    startDocRevision: async (draftId, instruction) => {
      const { currentCaseId, model, thinking, toast } = get()
      const text = instruction.trim()
      if (!text) return { ok: false, error: '请填写修订意见' }
      if (!currentCaseId) return { ok: false, error: '请先选择或新建案件' }
      const bridge = getBridge()
      if (!bridge) {
        return { ok: false, error: '当前为浏览器调试模式，无法发起文书修订' }
      }
      const uid = newMsgId()
      // 文书修订不支持随问附件：有遗留附件时提示并清空（不静默丢弃）
      if (get().pendingAttachments.length) {
        toast('随问附件仅在阅卷问答中生效，已清空本次附件')
        set({ pendingAttachments: [] })
      }
      set((s) => ({ chatting: true, input: '', messages: [...s.messages, { id: uid, role: 'user', text, createdAt: Date.now() }] }))
      try {
        const task = await bridge.agent.start({
          caseId: currentCaseId,
          mode: 'document_generation',
          input: text,
          // 修订分叉：不建新草稿，以当前稿为基准按意见 AI 定向改写，落 ai_revise 版本
          docgenInput: { templateId: '', docType: '', purpose: text, revisionDraftId: draftId, revisionInstruction: text },
          modelPolicy: { modelId: model, remote: true, reasoningEffort: thinking },
          conversationId: get().conversationId ?? undefined
        })
        set({ conversationId: task.conversationId ?? null })
        const tid = newMsgId()
        set((s) => ({
          messages: [
            ...s.messages,
            { id: tid, role: 'task' as const, taskId: task.id, lifecycle: task.lifecycle, steps: [], chat: [], startedAt: Date.now() }
          ]
        }))
        await startTaskFeed(tid, task.id)
        return { ok: true }
      } catch (e) {
        set((s) => ({ chatting: false, messages: s.messages.filter((m) => m.id !== uid) }))
        return { ok: false, error: e instanceof Error ? e.message : String(e) }
      }
    },

    openDocGen: (draftId) => {
      // 打开草稿：右侧预览区切到文书工作台（与中间区对话并存，升级方案 §7.1）；
      // 关闭草稿时回到文档列表。草稿产物仍走通用文档预览（openDoc）单独打开。
      set({
        docgenDraftId: draftId,
        rightView: draftId ? ('docgen' as const) : ('list' as const),
        rightHide: false,
        fullscreen: false,
        artOpen: false,
        citeTarget: null
      })
      persist()
    },

    setSkillDialogOpen: (v) => set({ skillDialogOpen: v }),

    setCaseRedactionPolicy: async (policy, customTypes) => {
      const bridge = getBridge()
      const caseId = get().currentCaseId
      if (!bridge || !caseId) {
        get().toast('当前为浏览器调试模式，无法保存脱敏设置')
        return false
      }
      try {
        await bridge.redaction.setPolicy({
          caseId,
          policy,
          customTypes: policy === 'custom' ? customTypes : undefined
        })
        await get().loadCases()
        const label = POLICY_LABEL[policy] ?? policy
        get().toast(`脱敏档位已切换：${label}（外发内容将按此档替换敏感信息）`)
        return true
      } catch (e) {
        get().toast(`保存失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    respondApproval: async (msgId, approvalId, decision) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        await bridge.approval.respond({ approvalId, decision: decision as 'allow_once' })
        set((s) => ({
          messages: s.messages.map((m) =>
            m.id === msgId && m.role === 'task' && m.approval
              ? { ...m, approval: { ...m.approval, decided: decision } }
              : m
          )
        }))
      } catch (e) {
        get().toast(`审批失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    answerHumanQuestions: async (msgId, taskId, questionSetId, answers) => {
      const bridge = getBridge()
      if (!bridge) return false
      try {
        await bridge.agent.answerHumanQuestions({ taskId, questionSetId, answers })
        // 卡片立即转只读摘要（事件回包同样会归约，重复归约幂等）
        set((s) => ({
          messages: s.messages.map((m) =>
            m.id === msgId && m.role === 'task' && m.questionSet?.id === questionSetId
              ? {
                  ...m,
                  lifecycle: 'RUNNING',
                  questionSet: { ...m.questionSet, status: 'answered' as const, answers, answeredAt: new Date().toISOString() }
                }
              : m
          )
        }))
        await startTaskFeed(msgId, taskId)
        return true
      } catch (e) {
        get().toast(`提交决策失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    loadPendingQuestions: async (msgId, taskId) => {
      const bridge = getBridge()
      if (!bridge) return
      try {
        const qs = await bridge.agent.getPendingQuestions({ taskId })
        if (!qs || qs.status !== 'pending') return
        set((s) => ({
          messages: s.messages.map((m) =>
            m.id === msgId && m.role === 'task' && !m.questionSet ? { ...m, questionSet: qs } : m
          )
        }))
      } catch {
        // 待答问题集是恢复入口的补充：拉取失败不阻断任务事件续看
      }
    },

    stopTask: async (msgId, taskId) => {
      // F-012：停止任务——同一对话可能残留多个旧任务，停止按钮一次收敛全部
      const bridge = getBridge()
      if (!bridge) return
      try {
        const activeIds = get()
          .messages
          .filter((m): m is Extract<Msg, { role: 'task' }> => m.role === 'task' && !TERMINAL.includes(m.lifecycle))
          .map((m) => m.taskId)
        const ids = [...new Set([taskId, ...activeIds])]
        await Promise.all(ids.map((id) => bridge.agent.cancel(id)))
        set((s) => ({
          messages: s.messages.map((m) =>
            m.role === 'task' && ids.includes(m.taskId) && !TERMINAL.includes(m.lifecycle)
              ? { ...m, lifecycle: 'CANCELLING' }
              : m
          )
        }))
      } catch (e) {
        get().toast(`停止失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },

    restartTask: (msgId) => {
      // 任务结束后重新编辑（停止/完成/失败均适用）：回填该轮消息文本到输入坞；旧消息保留，用户改完重新发送即新一轮任务
      const { messages, toast } = get()
      const idx = messages.findIndex((m) => m.id === msgId)
      if (idx < 0) return
      // task 消息紧随其对应的 user 消息插入（runReviewTask 等发起顺序），向前就近查找
      for (let i = idx - 1; i >= 0; i--) {
        const m = messages[i]
        if (m.role === 'user') {
          set({ input: m.text, openPop: null })
          toast('已回填消息文本，可编辑后重新发送')
          return
        }
        if (m.role === 'task') break // 跨任务边界停止，避免回填到更早一轮
      }
      toast('未找到可编辑的原始消息')
    },

    decideStrategy: async (taskId, planTitle) => {
      // REQ-STR-005：最终方案由用户确认，决定写入审计（AI 不自动选择）
      const bridge = getBridge()
      if (!bridge) {
        get().toast('当前为浏览器调试模式，无法记录方案决定')
        return false
      }
      try {
        await bridge.strategy.decide({ taskId, planTitle, decision: 'adopted' })
        get().toast(`已确认采用方案「${planTitle}」（已写入审计）`)
        return true
      } catch (e) {
        get().toast(`确认失败：${e instanceof Error ? e.message : String(e)}`)
        return false
      }
    },

    setLeftW: (w) => {
      set({ leftW: Math.max(LEFT_MIN, Math.min(LEFT_MAX, w)) })
      persist()
    },

    setRightW: (w) => {
      set({ rightW: Math.max(RIGHT_MIN, Math.min(Math.floor(window.innerWidth / 2), w)) })
      persist()
    },

    toggleLeft: () => {
      set((s) => ({ leftHide: !s.leftHide }))
      persist()
    },

    toggleRight: () => {
      set((s) => ({ rightHide: !s.rightHide }))
      persist()
    },

    resetLeft: () => {
      set({ leftW: DEF_LEFT, leftHide: false })
      persist()
    },

    resetRight: () => {
      set({ rightW: DEF_RIGHT, rightHide: false })
      persist()
    }
  }
})

/**
 * 将当前对话写入会话列表。
 * - close=true：会话结束（新对话/切换案件/切换模式/打开其他会话/关窗兜底），之后另开新会话；
 * - close=false：对话进行中增量落盘（任务到达终态时调用），当前对话实时出现在案件会话列表，
 *   且保留 activeSessionId 使后续增量更新原位进行、不产生重复会话。
 * 未终态任务随快照保留生命周期（后台继续执行）；仅应用关闭时标记为已取消。
 */
function upsertSession(close: boolean): void {
  const s = useApp.getState()
  const firstUser = s.messages.find((m): m is Extract<Msg, { role: 'user' }> => m.role === 'user')
  if (!firstUser) return
  // 增量落盘仅在无进行中任务时执行：等它结束后再落（后台路径会持续更新会话快照）。
  // 挂起等回答（WAITING_HUMAN）不是终态，但必须立即落盘：刷新/切回会话后要能恢复问题卡
  if (
    !close &&
    s.messages.some(
      (m) => m.role === 'task' && !TERMINAL.includes(m.lifecycle) && m.lifecycle !== 'WAITING_HUMAN'
    )
  )
    return
  const now = Date.now()
  const title = firstUser.text.trim().slice(0, 24) || '未命名对话'
  const snap = appClosing ? cancelNonTerminal(s.messages) : snapshotMessages(s.messages)
  let sessions: ChatSession[]
  let activeId = s.activeSessionId
  if (activeId && s.chatSessions.some((x) => x.id === activeId)) {
    // 继续已有会话：原位更新（conversationId 随会话留存，续聊时续传 Core）
    sessions = s.chatSessions.map((x) =>
      x.id === activeId
        ? { ...x, title, updatedAt: now, messages: snap, mode: s.mode, caseId: s.currentCaseId, conversationId: s.conversationId ?? x.conversationId }
        : x
    )
  } else {
    const sid = `s${now}-${msgSeq++}`
    activeId = sid
    sessions = [
      { id: sid, title, mode: s.mode, caseId: s.currentCaseId, updatedAt: now, messages: snap, conversationId: s.conversationId ?? undefined },
      ...s.chatSessions
    ]
    if (sessions.length > HISTORY_MAX) sessions = sessions.slice(0, HISTORY_MAX)
  }
  useApp.setState({ chatSessions: sessions, activeSessionId: close ? null : activeId })
  saveHistory(sessions)
}

/** 将当前对话存入历史（新对话/切换案件/切换模式/打开其他会话前调用；关闭应用前兜底保存） */
function saveSession(): void {
  upsertSession(true)
}

/** 应用正在关闭：关窗兜底保存时把未终态任务标记为已取消（Core 随进程结束，任务不可能再推进） */
let appClosing = false

// 关闭/刷新窗口前保存未入库的当前对话（直接同步写 localStorage）
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    appClosing = true
    saveSession()
  })
}

export { MODES, YJ_PRESETS, badge, LEFT_MIN, LEFT_MAX, RIGHT_MIN, DEF_LEFT, DEF_RIGHT }
export type { ModeKey, PresetKey }
