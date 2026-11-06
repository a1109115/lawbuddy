/**
 * @lawbuddy/contracts —— IPC / RPC / 事件 / 产物 Schema 的唯一类型来源。
 * 对应《02-技术设计方案》第 4、6、7、9、10、13、15 节。
 * Renderer（Zod 校验）与 Python Core（Pydantic 校验）分别由本文件生成/对齐。
 */

// ---------------------------------------------------------------------------
// 基础枚举
// ---------------------------------------------------------------------------

/** 三种工作模式，对应 PRD 第 13/14/15 章 */
export type AgentMode = 'legal_review' | 'contract_review' | 'document_generation'

/**
 * 模型思考强度（输入坞「模型」设置）：
 * off = 不启用扩展推理（不向网关下发参数，保持默认行为）；
 * 其余取值为直接下发给 OpenAI 兼容端点的 reasoning_effort 值。
 * 各模型实际支持哪些档位由 MODEL_THINKING_PROFILES 声明。
 */
export type ReasoningEffort = 'off' | 'low' | 'medium' | 'high' | 'max'

/**
 * 模型能力（思考档位 / 视觉 / 上下文 / 输出上限）不再由前端硬编码表提供：
 * 由 Core 模型注册表（services/core/lawbuddy/agent/model_registry.py）统一下发，
 * 见下方 ModelProviderInfo / ProviderModelInfo。内置服务商按官方 API 文档预设，
 * 用户自定义服务商与自定义模型同样走该目录，前后端只有一份事实来源。
 */

/** 输入坞随问附件（临时性补充材料）：不导入案件文档列表，不 OCR，随任务直发模型 */
export interface ChatAttachment {
  /** 展示名（含扩展名；剪贴板截图为「截图_时间戳.png」） */
  name: string
  /** MIME 类型（image/* 走视觉通道；text/* 并入提示文本） */
  mime: string
  /** 文件内容 base64（不含 data: 前缀） */
  dataBase64: string
}

/** 任务生命周期，对应技术方案 4.2 状态图 */
export type TaskLifecycle =
  | 'CREATED' | 'INGESTING' | 'READY' | 'PLANNING' | 'RUNNING'
  | 'WAITING_APPROVAL' | 'WAITING_HUMAN' | 'PAUSED' | 'CANCELLING'
  | 'CANCELLED' | 'VERIFYING' | 'COMPLETED' | 'COMPLETED_WITH_WARNINGS' | 'FAILED'

/** 引证 / Claim 状态，对应 PRD 7.5 */
export type ClaimStatus =
  | 'supported' | 'partially_supported' | 'conflict' | 'needs_human' | 'stale'

export type ClaimType = 'fact' | 'law' | 'inference' | 'recommendation'

/** 文档处理状态，对应 PRD 7.3 */
export type DocumentStatus =
  | 'pending' | 'security_check' | 'need_password' | 'decrypting'
  | 'parsing' | 'ocr_queued' | 'ocr_running' | 'indexing'
  | 'ready' | 'partial' | 'locked' | 'stale' | 'failed'

/** 能力枚举，对应技术方案 7.1。ORIGINAL_MODIFY 永久禁止，不在此列出 */
export type Capability =
  | 'CASE_DOCUMENT_READ' | 'CASE_DERIVED_WRITE' | 'DOCUMENT_DECRYPT'
  | 'NETWORK_MODEL' | 'RAW_DOCUMENT_EGRESS'
  | 'ARTIFACT_EXPORT' | 'CLIPBOARD_WRITE' | 'PLUGIN_INSTALL' | 'TEMPLATE_MANAGE'

/** 权限预设，对应 PRD 12.5 */
export type PermissionPreset = 'strict_local' | 'controlled_external' | 'custom'

/** 脱敏档位（REQ-PRIV-002 / 技术方案 11.3）：light 五类直接标识符 / standard 追加姓名 / strict 全类型（仅个人敏感信息，机构名、案号、信用代码不脱敏）/ custom 自选 */
export type RedactionPolicy = 'light' | 'standard' | 'strict' | 'custom'

/** 脱敏实体状态：confirmed 已确认；needs_human 低置信待人工确认（外发阻断，REQ-PRIV-006） */
export type RedactionEntityStatus = 'confirmed' | 'needs_human'

// ---------------------------------------------------------------------------
// 核心对象（对应技术方案 13.1 表结构）
// ---------------------------------------------------------------------------

export interface WorkspaceInfo {
  id: string
  name: string
  rootUri: string
  createdAt: string
}

export interface CaseInfo {
  id: string
  workspaceId: string
  title: string
  /** 关联案件（文书生成任务只读共享其材料与案件记忆；null 表示未关联） */
  linkedCaseId: string | null
  caseType: 'criminal' | 'civil' | 'commercial' | 'administrative' | 'contract' | 'other'
  clientName: string | null
  clientRole: string | null
  stage: string | null
  defaultMode: AgentMode
  modeConfig?: { contractType?: string; reviewBackground?: string }
  status: 'draft' | 'active' | 'restricted' | 'archived' | 'pending_delete'
  pinned: boolean
  permissionPreset: 'strict_local' | 'controlled_external' | 'custom'
  /** 案件脱敏档位（V10 / REQ-PRIV-004），默认 standard */
  redactionPolicy: RedactionPolicy
  /** custom 档勾选的敏感字段类型（其他档为空数组） */
  redactionCustomTypes: string[]
  createdAt: string
  updatedAt: string
  archivedAt: string | null
}

export interface CaseCreateInput {
  title: string
  caseType: CaseInfo['caseType']
  clientName: string
  clientRole?: string
  stage?: string
  defaultMode?: AgentMode
  /** 文书生成任务可关联同工作空间的既有案件，创建后只读共享其材料与案件记忆 */
  linkedCaseId?: string
  permissionPreset?: CaseInfo['permissionPreset']
  clientRequestId?: string
  modeConfig?: { contractType?: string; reviewBackground?: string }
}

export interface ContractOverviewConfig {
  primaryDocumentId?: string
  primaryVersionId?: string
  stance?: string
  reviewIntensity: 'basic' | 'standard' | 'deep'
  limitedScopeAcknowledged?: boolean
  contractType?: string
  transactionGoal?: string
  governingLaw?: string
}

export interface ContractOverview {
  id: string
  caseId: string
  revision: number
  config: Partial<ContractOverviewConfig>
  parseStatus: 'no_document' | 'missing' | 'stale' | 'limited_scope' | 'ready' | 'parsing'
  pageCount: number
  updatedAt: string
  latestChecklistTaskId: string | null
}

export interface ContractChecklistItem {
  id: string
  title: string
  criterion: string
  category: 'subject' | 'business' | 'legal' | 'completeness' | 'custom'
  enabled: boolean
  origin: 'user' | 'ai'
  relatedSources: SourceRef[]
}

export interface ContractChecklistSnapshot {
  id: string
  overviewId: string
  revision: number
  overviewRevision: number
  items: ContractChecklistItem[]
  status: 'draft' | 'confirmed' | 'stale'
  confirmedAt: string | null
  hash: string | null
}

export interface ContractChecklistResult {
  itemId: string
  title: string
  status: 'risk' | 'pass' | 'needs_human' | 'not_applicable'
  reason: string
  reasonCode?: string
  cite: CiteChip | null
  risks: Array<{
    id: string
    riskLevel: 'high' | 'medium' | 'low'
    riskNote: string
    originalText: string
    pageNo: number | null
    /** 引文含省略/改述时，实际匹配到的原文片段（仅用于定位，不代表证据已支持） */
    locatorQuote?: string
    clauseRef: string
    impact: string
    suggestion: string
    replacementText: string
    applications?: Array<{ targetId: string; revision: number; applyState: 'applied' }>
    cite: CiteChip
  }>
}

export interface ContractRiskRun {
  id: string
  task: AgentTaskInfo
  snapshotId: string
  reviewId: string | null
  status: 'created' | 'running' | 'partial' | 'completed' | 'stale'
  items: ContractChecklistResult[]
  total: number
  limitedScope: boolean
}

export interface BestPracticeTemplate {
  stableId: string
  templateId: string
  title: string
  docType: string
  category: string
  fields: Array<{ name: string; label: string; required: boolean; sourcePolicy: 'confirm_or_evidence' }>
  outline: string[]
  writingChecks: string[]
  sourceMap: { sourceName: string; sha256: string; chapterStartLine: number }
  reviewStatus: 'needs_human' | 'reviewed'
  version: number
}

export interface PromptDocument {
  schemaVersion: 1
  templateId: string
  templateVersion: number
  segments: Array<
    { type: 'text'; text: string } |
    { type: 'field'; name: string; label: string; required: boolean }
  >
  fieldValues: Record<string, string>
}

export interface WordNode {
  type: 'doc' | 'paragraph' | 'heading' | 'text' | 'hardBreak' | 'bulletList' | 'orderedList' | 'listItem' | 'table' | 'tableRow' | 'tableCell' | 'tableHeader'
  attrs?: {
    level?: number
    textAlign?: string
    start?: number
    colspan?: number
    rowspan?: number
    colwidth?: number[] | null
    /** 首行缩进（pt；来自 DOCX w:ind / firstLineChars，用于预览与编辑态排版一致） */
    firstLineIndent?: number | null
    /** 段前间距（pt） */
    spaceBefore?: number | null
    /** 段后间距（pt） */
    spaceAfter?: number | null
    /** 固定行距（pt；来自 DOCX w:spacing lineRule=exact） */
    lineSpacing?: number | null
  }
  text?: string
  marks?: Array<{
    type:
      | 'bold'
      | 'italic'
      | 'underline'
      | 'strike'
      | 'textStyle'
      /** 修订：插入（对应 DOCX w:ins） */
      | 'insertion'
      /** 修订：删除（对应 DOCX w:del，正文保留以便接受/拒绝） */
      | 'deletion'
      /** 批注锚点（对应 DOCX commentRange/commentReference） */
      | 'comment'
    attrs?: {
      fontFamily?: string | null
      fontSize?: string | null
      lineHeight?: string | null
      /** 文字颜色（#RRGGBB） */
      color?: string | null
      /** 文字背景高亮（#RRGGBB） */
      backgroundColor?: string | null
      /** 修订/批注作者（insertion/deletion/comment） */
      author?: string | null
      /** 修订/批注时间（ISO 8601，insertion/deletion/comment） */
      date?: string | null
      /** 修订/批注标识（insertion/deletion/comment；同一批注在多个段落共用） */
      revisionId?: string | null
      /** 批注正文（comment） */
      body?: string | null
    }
  }>
  content?: WordNode[]
}

export interface WordWorkCopy {
  id: string
  caseId: string
  title: string
  revision: number
  model: WordNode
  sourceVersionId: string | null
  documentId: string | null
  draftId: string | null
  warnings?: string[]
}

export interface DocumentMeta {
  id: string
  caseId: string
  title: string
  mime: string | null
  kind: 'pdf' | 'docx' | 'xlsx' | 'pptx' | 'txt' | 'md' | 'html' | 'png' | 'jpg' | 'tiff' | string
  status: DocumentStatus
  activeVersionId: string | null
  pageCount: number | null
  ocrPendingPages: number
  /** 解析文本已原地脱敏（原件二进制不动；文本视图/Agent/检索使用打码后文本） */
  isRedacted?: boolean
  sha256: string | null
  sizeBytes: number | null
  createdAt: string
  updatedAt: string
}

/** document.redact_preview 返回：脱敏前按类型的命中计数（不含原文明细） */
export interface RedactPreviewResult {
  documentId: string
  total: number
  counts: Record<string, number>
  pageCount: number
}

/** document.redact_apply 返回：原地脱敏（existing=true 表示已脱敏过，直接幂等返回） */
export interface RedactApplyResult {
  documentId: string
  title: string
  pageCount: number
  maskedCount: number
  existing: boolean
}

/** 单页文本（页级定位的基础，Q-001 的前置数据） */
export interface DocumentPage {
  pageNo: number
  printedPage: string | null
  text: string
  quality: 'native' | 'ocr' | 'ocr_pending' | 'empty'
  /** OCR 置信度（无 OCR 为 null） */
  ocrConfidence?: number | null
  /** 是否经过人工更正（O-008：原值保留在更正历史中） */
  corrected?: boolean
  correctionCount?: number
}

/** 页内 OCR 文本行（含页面归一化 bbox，用于原件预览按原文片段定位） */
export interface DocumentTextBlock {
  order: number
  type: string
  /** 页面宽高归一化坐标 [x1, y1, x2, y2]，左上角为原点 */
  bbox: [number, number, number, number] | null
  text: string
  confidence: number | null
}

/** 单文件导入结果（成功 / 案件内重复 / 失败） */
export interface ImportOutcome {
  name: string
  /** need_password：文件已加密，需用户经专用窗口提供密码（REQ-DOC-004） */
  status: 'imported' | 'duplicate' | 'failed' | 'need_password'
  documentId?: string
  title?: string
  kind?: string
  pageCount?: number
  reason?: string
}

/**
 * 批量导入进度（分批导入时每批完成后推送）。
 * 单批文件数/体积有上限，避免一次 RPC 长时间占住 Core 的 stdin 循环；
 * Renderer 收到后即可增量刷新文档列表，无需等全部文件导入完成。
 */
export interface DocumentImportProgress {
  caseId: string
  /** 已处理文件数（含成功、重复、失败、待解锁） */
  done: number
  /** 本次导入的文件总数 */
  total: number
  /** 刚完成批次中的最后一个文件名（用于「正在导入」提示） */
  current?: string
}

/** 加密文档解锁结果（REQ-DOC-004 / 011） */
export interface DocumentUnlockResult {
  unlocked: boolean
  documentId: string
  /** 解锁后内容的 SHA-256（原件摘要不变，REQ-DOC-002） */
  sha256: string
  /** 解锁后解析出的页数 */
  pageCount: number
}

/** 审计日志条目（REQ-AUD-001：只追加哈希链，仅元数据摘要） */
export interface AuditLogEntry {
  seq: number
  createdAt: string
  actor: string
  action: string
  resourceType: string
  resourceId: string
  caseId: string | null
  result: 'ok' | 'denied' | 'failed'
  metadata: Record<string, unknown>
  eventHash: string
}

export interface AuditVerifyResult {
  ok: boolean
  brokenAt?: number
  total: number
  reason?: string
}

/** 全文检索命中（DEV-DOC-008：案件强制过滤 + BM25，命中保留检索方式与分数） */
export interface SearchHit {
  documentId: string
  documentTitle: string
  versionId: string
  pageNo: number
  quality: string
  snippet: string
  score: number
  matchType: 'fts' | 'like'
}

/** OCR 任务状态（DEV-DOC-006：按页进度 / 暂停 / 恢复 / 取消 / 断点续跑） */
export interface OcrTaskStatus {
  documentId: string
  state: 'idle' | 'running' | 'paused' | 'cancelled' | 'done' | 'failed'
  total: number
  done: number
  failed: number
  pending: number
  currentPage: number | null
  lowConfPages: number
}

/** Agent 任务（DEV-AGT-001，技术方案 §4.1/4.2） */
export interface AgentTaskInfo {
  id: string
  caseId: string
  mode: AgentMode
  lifecycle:
    | 'CREATED' | 'PLANNING' | 'RUNNING' | 'WAITING_APPROVAL' | 'WAITING_HUMAN'
    | 'PAUSED' | 'CANCELLING' | 'CANCELLED' | 'VERIFYING'
    | 'COMPLETED' | 'COMPLETED_WITH_WARNINGS' | 'FAILED'
  stage: string | null
  request: string
  /** 会话持久化锚点（V26）：同会话多轮任务共享全文历史；前端随下次 agent.start 续传 */
  conversationId: string | null
  /** 法律阅卷工作流预设（PRD 13：四类功能入口） */
  workflowPreset: 'review_analysis' | 'visualization' | 'evidence_analysis' | 'strategy' | null
  modelPolicy: Record<string, unknown>
  toolPolicy: Record<string, unknown>
  createdAt: string
  finishedAt: string | null
}

/** 任务事件（单调递增 seq，getEvents(afterSeq) 增量续订） */
export interface AgentTaskEvent {
  seq: number
  type:
    | 'status'
    /** 自主 Agent Loop 六事件协议（autonomous.py：UI 纯事件驱动渲染） */
    | 'text' | 'tool_use_start' | 'tool_use_done'
    | 'tool_use_progress'
    /** 模型深度思考进行中（reasoning 增量节流聚合，防长思考期「假卡死」） */
    | 'thinking_progress'
    | 'assistant_message' | 'tool_result_message' | 'turn_complete'
    /** 会话级任务清单更新（task_plan 工具输出后；覆盖式，取最新） */
    | 'plan_update'
    /** 检索子代理进度（case_explore 执行中，挂到外层工具条目 callId 上） */
    | 'explore_progress'
    /** 收尾引用校验（verify.py：角标数据源，悬浮出处卡 + 待核实状态） */
    | 'answer_verification'
    | 'error'
    /** 遗留固定 Loop 事件（历史任务回放仍会出现） */
    | 'plan' | 'tool_call' | 'tool_result' | 'model_call'
    | 'approval_request' | 'approval_decision' | 'message' | 'message_delta'
    /** 预算与无进展控制事件（DEV-AGT-006：预警 / 拦截 / NO_PROGRESS） */
    | 'budget'
    /** 案件记忆入库事件（DEV-AGT-007） */
    | 'memory'
    /** 外发脱敏事件（GOAL-004：先脱敏再授权） */
    | 'redaction'
    /** 阅卷 Loop 事件（05-阅卷Agent升级方案：任务卡 / 语料 / 通读 / 记忆 / 校验 / 恢复） */
    | 'task_card' | 'corpus' | 'reading' | 'case_memory' | 'verification' | 'resume'
    /** 人工介入请求（历史任务） */
    | 'human_intervention'
    /** 文书生成需要用户决策：下发问题集并暂停任务（WAITING_HUMAN） */
    | 'human_question'
    /** 用户已回答问题，任务从同一检查点恢复 */
    | 'human_answer' | 'task_resumed'
    /** 文书生成案件上下文准备完成（只含计数元数据，不含原文） */
    | 'case_context'
  payload: Record<string, unknown>
  createdAt: string
}

// ---- 人机问题（文书生成必要决策；暂停 / 回答 / 恢复） ----

/** 问题选项：整行可点击；description 说明选择后果（一行内） */
export interface HumanQuestionOption {
  id: string
  label: string
  /** 选择该选项的后果或影响（短句，帮助用户判断） */
  description?: string
  /** 推荐项（至多一个；需在 recommendationReason 说明推荐依据） */
  recommended?: boolean
  recommendationReason?: string
}

/** 单个问题：一次只问会改变下一步生成行为的问题 */
export interface HumanQuestion {
  id: string
  /** 问题标题（卡片顶部） */
  title: string
  /** 为什么现在需要决定（说明不回答会影响哪一步生成） */
  reason?: string
  kind: 'single_choice' | 'multi_choice' | 'text'
  options?: HumanQuestionOption[]
  /** 允许「其他」自由输入 */
  allowOther?: boolean
  /** 允许「暂不决定」并说明影响 */
  allowDefer?: boolean
  required?: boolean
  /** 该决策影响的文书部分（如「诉讼请求」「辩护方向」），用于回答后回显 */
  affects?: string
}

/** 用户对单个问题的回答 */
export interface HumanAnswer {
  questionId: string
  optionIds?: string[]
  /** 其他/文本类回答 */
  text?: string
  /** 用户选择暂不决定（Core 记为 pending_verify，不伪造结论） */
  deferred?: boolean
}

/** 提问状态：pending 待回答 / answered 已回答 / cancelled 任务取消 */
export type HumanQuestionSetStatus = 'pending' | 'answered' | 'cancelled'

/** 问题集（任务暂停时持久化；刷新或重启后可恢复待答卡片） */
export interface HumanQuestionSet {
  id: string
  taskId: string
  conversationId: string | null
  /** 关联草稿（已建草稿时携带，回答后从同一草稿继续） */
  draftId?: string | null
  /** 卡片顶部上下文标题（如「文书生成需要你的决策」） */
  title: string
  /** 触发提问的阶段说明（如「已通读关联案件材料，需确认诉讼立场」） */
  contextDigest?: string
  questions: HumanQuestion[]
  status: HumanQuestionSetStatus
  /** 恢复所用检查点标识（Core 保证从同一检查点续跑，不重复副作用） */
  checkpointId?: string | null
  createdAt: string
  answeredAt?: string | null
  answers?: HumanAnswer[]
}

/** human_question 事件载荷 */
export interface HumanQuestionPayload {
  questionSet: HumanQuestionSet
}

/** human_answer / task_resumed 事件载荷 */
export interface HumanAnswerPayload {
  questionSetId: string
  answers: HumanAnswer[]
  /** 用户决策写入草稿槽位来源的条数（user_confirmed） */
  confirmedSlots?: number
  resumed?: boolean
}

// ---- 自主 Agent Loop 事件 payload（6 种） ----

/** text：模型输出文本增量（流式，追加式拼接） */
export interface TextDeltaPayload {
  delta: string
}

/** tool_use_start：即将执行某工具 */
export interface ToolUseStartPayload {
  callId: string
  name: string
  arguments: Record<string, unknown>
}

/** tool_use_progress：工具参数流式生成中（300ms 节流；同 callId 后续 tool_use_start 覆盖） */
export interface ToolUseProgressPayload {
  callId: string
  name: string
  chars: number
}

/** thinking_progress：模型深度思考进行中（1s 节流；assistant_message 到达即本轮思考结束） */
export interface ThinkingProgressPayload {
  /** 对话轮次（从 1 起） */
  round: number
  /** 本轮已累计思考字符数（仅统计，不含思考内容） */
  chars: number
}

/** plan_update：会话级任务清单更新（task_plan 工具输出后；覆盖式，取最新） */
export interface PlanUpdatePayload {
  steps: Array<{ text: string; status: string }>
}

/** explore_progress：检索子代理（case_explore）执行进度，挂到外层工具条目 */
export interface ExploreProgressPayload {
  callId: string
  /** 已读取的不同文档数 */
  reads: number
  /** 检索次数（document_search + memory_search） */
  searches: number
}

/** tool_use_done：工具执行结束 */
export interface ToolUseDonePayload {
  callId: string
  name: string
  status: 'ok' | 'error'
}

/** assistant_message：模型一轮完整输出 */
export interface AssistantMessagePayload {
  /** 对话轮次（从 1 起） */
  round: number
  /** 本轮完整文本 */
  content: string
  /** 本轮发起的工具调用明细 */
  toolCalls: Array<{ id: string; name: string; arguments: string }>
}

/** tool_result_message：工具结果消息 */
export interface ToolResultMessagePayload {
  callId: string
  name: string
  status: 'ok' | 'error'
  result: unknown
}

/** 完成守门警告项（05 方案 §7.1/§7.3：机器可读 reason code + 人类可读说明） */
export interface FinalizeWarning {
  /** 警告原因码：PENDING_CITATIONS / UNREAD_PAGES / MISSING_DELIVERABLE / EMPTY_ANSWER 等 */
  code: string
  /** 人类可读说明（UI 直接展示，需同时配状态图标，不只靠颜色） */
  text: string
  /** 声称生成但未落盘的文件名（MISSING_DELIVERABLE 时给出） */
  files?: string[]
}

/** 阅读覆盖统计（仅 full_review 深度要求覆盖；其他深度 required=false） */
export interface FinalizeCoverage {
  required: boolean
  totalPages: number
  readPages: number
  /** 未读到终态的页数 */
  unread: number
  /** 用户明确排除、带理由通过的页数（05 方案 §5.4 excluded 终态） */
  excluded?: number
}

/** 完成守门结论：ok=false 时任务以 COMPLETED_WITH_WARNINGS 收束 */
export interface FinalizeResult {
  ok: boolean
  /** 任务深度：direct / focused / full_review（05 方案 §5.3） */
  taskDepth: string
  warnings: FinalizeWarning[]
  coverage: FinalizeCoverage
}

/** 子 Agent 使用情况（05 方案 §4.2 / §10.4 D4） */
export interface EfficiencySubagents {
  /** 实际派发次数（不含缓存命中与预算拒绝） */
  dispatched: number
  /** 相同调查问题命中缓存、未重复派发的次数 */
  cached: number
  /** 因共享预算耗尽被拒绝的派发次数 */
  denied: number
  /** 子代理累计消耗的模型轮次 */
  rounds: number
  /** 子代理返回的结构化候选条目数 */
  findings: number
  /** 其中以同一原文出处进入证据账本的条数 */
  findingsAdopted?: number
  /** 采用率 = findingsAdopted / findings（无候选时为 null） */
  adoptionRate?: number | null
}

/**
 * 任务效率指标（05 方案 §4.2 / §10.4 D4）：定位重复读取、无效检索与子 Agent 空转。
 * 只度量本次运行行为，不改变结论内容。
 */
export interface TurnEfficiency {
  toolCalls: number
  toolCallsByName: Record<string, number>
  /** 读页调用次数（主 Agent 与子 Agent 合计） */
  reads: number
  readsByOrigin: Record<string, number>
  /** 重新读取已完整读过页面的调用次数 */
  repeatedReads: number
  /** 重复读取涉及的页数合计 */
  repeatedReadPages: number
  searches: number
  searchesByOrigin: Record<string, number>
  /** 无命中检索次数 */
  emptySearches: number
  /** 重复检索同一检索词的次数（命中结果直接复用） */
  duplicateSearches: number
  /** 检索命中去重后的页数 */
  uniqueHitPages: number
  subagents: EfficiencySubagents
}

/** turn_complete：整个任务循环结束（loading 收尾） */
export interface TurnCompletePayload {
  rounds: number
  /** final_answer = 模型给出最终回答；max_rounds = 达到最大轮数；max_tool_calls = 累计工具调用达到上限；empty_answer = 连续空回答 */
  reason: 'final_answer' | 'max_rounds' | 'max_tool_calls' | 'empty_answer'
  /** 轮数用尽后追发无工具调用强制作答（结论已产出） */
  forcedFinal?: boolean
  promptTokens?: number
  completionTokens?: number
  durationMs?: number
  /**
   * 各推理阶段（extract / synthesis / repair）实际调用次数（05 方案 §10.4 D3）：
   * 用于确认按阶段收敛思考档位没有把工作挪走或省掉，只是降了单轮成本
   */
  reasoningStages?: Record<string, number>
  /** 任务效率指标（05 方案 §4.2 / §10.4 D4）：重复读取、无效检索、子 Agent 采用率 */
  efficiency?: TurnEfficiency
  /** 完成守门结论（05 方案 §7.1）：缺省表示旧任务事件，UI 按干净完成处理 */
  finalize?: FinalizeResult
}

/** human_intervention 事件 payload（§11.3：确认后重新发起即从断点续跑） */
export interface HumanInterventionPayload {
  stage: string
  reason: string
  blockedTypes: Record<string, number>
  action: string
}

/** 任务解释卡（阅卷升级方案 §7.1：任务理解先于结论） */
export interface ReviewTaskCard {
  goal: string
  deliverables: string[]
  clientPosition: string
  mustInclude: string[]
  completionCriteria: string[]
  openQuestions: string[]
  degraded?: string
}

/** 材料阅读分档：full=全文通读 summary=仅首尾页 retrieval=索引类检索待命 */
export type DocumentReadingTier = 'full' | 'summary' | 'retrieval'

/** corpus 事件 payload（§5.2：语料边界与阅读队列） */
export interface CorpusPayload {
  documents: number
  units: number
  unread: number
  corpusVersion: string
  titles: string[]
  unreadDetails: Array<{ documentId: string; documentTitle: string; reason: string }>
  /** 分档文档数（效率优先分档通读，2026-09-24） */
  tiers?: { full: number; summary: number; retrieval: number }
  /** 分档阅读单元数（retrieval 档不入队，恒为 0 而不出现） */
  tierUnits?: { full: number; summary: number }
  /** 检索待命文档：未入阅读队列，交付结果中需如实列出 */
  deferredDetails?: Array<{ documentId: string; documentTitle: string; reason: string }>
}

/** reading 事件 payload（§6：全案通读进度，按阅读单元计） */
export interface ReadingProgressPayload {
  read: number
  total: number
  coveragePct: number
  lastUnit?: string
  observations?: number
  stage?: 'degraded' | 'started'
  reason?: string
  impact?: string
  /** single_pass=整卷单次（仅短卷宗）；batch=分批并行 */
  mode?: 'single_pass' | 'batch' | 'single_pass_fallback'
  /** 本波批次数量（>1 表示并行抽取） */
  parallelBatches?: number
  /** 本波批内阅读单元数 */
  batchUnits?: number
  /** 待读语料字符数（规模提示） */
  corpusChars?: number
  /** 未读单元数（整卷单次失败回退时上报） */
  units?: number
  /** 分档阅读单元数 */
  tiers?: { full: number; summary: number }
  durationMs?: number
}

/** case_memory 事件 payload（§5.3：案件工作记忆统计） */
export interface CaseMemoryPayload {
  timeline: number
  entities: number
  keyFacts: number
  contradictions: number
  evidence: number
  openQuestions: number
  legalResearch: number
  degraded?: string
}

/** verification 事件 payload（§9：质量门禁与完成条件核验） */
export interface VerificationReportPayload {
  readingCoverage: string
  coveragePct: number
  unreadWithReason: number
  documents: number
  observations: number
  legalResearch: number
  openQuestions: number
  warnings?: string[]
}

/**
 * 模型服务商 / 模型目录（由 Core `model.catalog` 下发，前端不再硬编码）。
 *
 * - 内置服务商（deepseek / zhipu / qwen / kimi）按各厂商官方 API 文档预设
 *   base 地址、请求路径、协议与模型目录，用户只需填写 API Key；
 * - 「自定义设置」中的 API 地址与模型目录覆盖存 Core settings，不改预设本身；
 * - 自定义服务商（OpenAI Chat Completions 兼容）由用户在界面新增。
 * - API Key 只以 configured 布尔值下发，任何接口都不回传密钥明文。
 */
export interface ProviderModelInfo {
  /** 模型 ID（请求体 model 字段） */
  id: string
  /** 展示名称 */
  label: string
  /** 上下文窗口（token）；0 = 用户留空，由网关按最佳默认值处理 */
  contextLimit: number
  /** 单次最大输出 token 数；0 = 用户留空，由网关按最佳默认值处理 */
  maxOutput: number
  /** 是否支持图片输入 */
  vision: boolean
  /** 支持的思考档位（空数组 = 不支持档位调节） */
  reasoningLevels: ReasoningEffort[]
  /**
   * 用户是否启用推理（缺省 = 启用）。
   * 用于区分「从未配置过档位的老数据」（应按默认开启展示）与
   * 「用户主动关闭了推理」（必须保持关闭，不能每次打开设置都被重置）。
   */
  reasoningEnabled?: boolean
  /** 未指定档位时的默认档位（无档位为空字符串） */
  reasoningDefault: ReasoningEffort | ''
  /** 档位 → 实际下发的 API 参数值（如 medium → medium） */
  reasoningParams: Partial<Record<ReasoningEffort, string>>
}

/** 服务商 API 协议（自定义服务商可选） */
export type ModelProtocol = 'openai' | 'openai-responses' | 'anthropic'

/**
 * 服务商的官方推理档位预设（Core 按各厂商 API 文档声明）：
 * 新建模型时用它生成档位行，避免把 low/medium/high 套到只接受
 * low/high/max 的服务商（如智谱 GLM-5.3）而必然 400。
 */
export interface ReasoningPreset {
  /** 官方档位（顺序即强弱顺序，最低档用于读页降档） */
  levels: ReasoningEffort[]
  /** 默认档位 */
  default: ReasoningEffort | ''
  /** 档位 → 官方 API 参数值（如 deepseek off → none） */
  params: Partial<Record<ReasoningEffort, string>>
}

export interface ModelProviderInfo {
  /** 服务商 ID（自定义服务商由用户填写，小写字母开头） */
  id: string
  label: string
  /** API 地址（OpenAI 兼容 base，含 /v1 等版本段） */
  base: string
  /** 请求路径（如 /chat/completions、/responses、/messages） */
  path: string
  /** API 协议：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages */
  protocol: ModelProtocol
  /** 该服务商 API Key 的 Core 设置键（model.<id>ApiKey） */
  keySetting: string
  /** 是否内置预设（不可删除，只能恢复默认） */
  builtin: boolean
  /** 是否已配置可用密钥（由用户自行配置） */
  configured: boolean
  /** 是否支持原生 JSON mode */
  jsonMode: boolean
  /** 是否下发 reasoning_effort（千问等用 enable_thinking 的服务商为 false） */
  sendReasoningEffort: boolean
  /** 服务商默认单次输出上限（模型未声明时使用） */
  maxTokens: number
  /**
   * 该服务商官方 API 文档声明的推理档位（如智谱 / Kimi 为 low、high、max）：
   * 新建模型、开启推理等级时按此生成档位与 API 参数值。
   */
  reasoningPreset: ReasoningPreset
  /** 用户是否覆盖过 API 地址或模型目录 */
  customized?: boolean
  models: ProviderModelInfo[]
}

/** 模型目录快照（model.catalog 返回值） */
export interface ModelCatalog {
  providers: ModelProviderInfo[]
  /** 当前选中的服务商 ID */
  defaultProvider: string
}

/** 保存服务商（内置 = 写覆盖；自定义 = 新增/更新） */
export interface SaveProviderRequest {
  provider: {
    id: string
    label: string
    base: string
    /** API 协议（自定义服务商可选；缺省 openai） */
    protocol?: ModelProtocol
    /** 缺省按协议取默认路径（openai → /chat/completions） */
    path?: string
    jsonMode?: boolean
    sendReasoningEffort?: boolean
    maxTokens?: number
    models: Array<Partial<ProviderModelInfo> & { id: string }>
  }
  /** 同一次保存提交的 API Key；空串表示清除。缺省不改动已保存的密钥 */
  apiKey?: string
}

/** 「获取可用模型」探测结果（只含公开元数据） */
export interface FetchedModelInfo {
  id: string
  label: string
  contextLimit: number
  maxOutput: number
}

/** 服务商连通性测试结果 */
export interface ModelTestResult {
  ok: boolean
  latencyMs: number
  message: string
}

/** message_delta 事件 payload（P-010：text 为累积原始文本，preview 为可读成稿片段） */
export interface MessageDeltaPayload {
  /** 累积原始文本（覆盖式；重连恢复无需拼接） */
  text: string
  /** 已完成字段值拼成的可读片段（仅预览，最终以 message 事件为准） */
  preview?: string
  /** FR-002：流式中断，内容不完整 */
  incomplete?: boolean
}

/** Core 主动推送的任务事件通知（REQ-AI-001：推送 + 轮询兜底；按 seq 去重） */
export interface AgentEventNotification {
  taskId: string
  event: AgentTaskEvent
}

/** 审批卡内容（PRD 12.5：用途/工具/目标/数据摘要/授权期限/拒绝后降级） */
export interface ApprovalCard {
  approvalId: string
  capability: string
  target: string
  dataSummary: string
  payloadHash: string
  expiresInMinutes: number
  fallback: string
}

/** 引证芯片（Claim → SourceRef 校验结果，报告条目内嵌） */
export interface CiteChip {
  claimId?: string
  claimStatus?: 'supported' | 'needs_human' | string
  reasonCode?: string | null
  documentId?: string
  pageNo?: number
  label?: string
  quote?: string
  documentVersionId?: string
}

export interface CitationTarget {
  documentId: string
  documentVersionId?: string
  pageNo: number
  quote?: string
}

/** 阅卷报告全规格（PRD 13.3 十一章节；带 sourceDoc 条目经 Claim Ledger 校验） */
export interface ReviewReport {
  overview: string
  parties: Array<{ name: string; role: string; note?: string; sourceDoc?: string; cite?: CiteChip }>
  timeline: Array<{ date: string; event: string; sourceDoc?: string; cite?: CiteChip }>
  disputes: Array<{ title: string; desc: string; sourceDoc?: string; cite?: CiteChip }>
  facts: Array<{
    text: string
    stance: 'favorable' | 'unfavorable' | 'neutral'
    status: 'confirmed' | 'pending'
    sourceDoc?: string
    cite?: CiteChip
  }>
  contradictions: Array<{
    topic: string
    sideA: string
    sideB: string
    note?: string
    /** 矛盾双侧引证（R-004：两侧都有引用） */
    citeA?: CiteChip
    citeB?: CiteChip
  }>
  missing: string[]
  legalBasis: Array<{ title: string; point: string; sourceDoc?: string; cite?: CiteChip }>
  nextSteps: string[]
}

/** 时间线节点（TimelineSpec，PRD 13.4 / ADR-005 受限 Schema；节点经 Claim 校验） */
export interface TimelineNode {
  date: string
  event: string
  actors: string[]
  sourceDoc?: string
  cite?: CiteChip
  status?: 'supported' | 'needs_human' | 'pending' | string
}

export interface TimelineSpec {
  nodes: TimelineNode[]
}

/** 人物关系图节点与边（RelationGraphSpec，受限 Schema） */
export interface RelationNode {
  name: string
  kind: 'person' | 'org' | string
  role?: string
  note?: string
  sourceDoc?: string
  cite?: CiteChip
  status?: 'supported' | 'needs_human' | 'pending' | string
}

export interface RelationEdge {
  from: string
  to: string
  label: string
  sourceDoc?: string
  cite?: CiteChip
  status?: 'supported' | 'needs_human' | 'pending' | string
}

export interface RelationGraphSpec {
  nodes: RelationNode[]
  edges: RelationEdge[]
}

/**
 * 流程/证据关系图节点（FlowSpec，mermaid 渲染）。
 * id 由模型给出、客户端重映射为序号后确定性生成 DSL（ADR-005：模型文本只进 label，
 * 不直接产出可解析的 DSL 或脚本）；每个节点/边经 Claim 校验。
 */
export interface FlowNode {
  id: string
  label: string
  /** mermaid 节点形状：rect 矩形 / stadium 起止 / round 圆角 / diamond 判断 / circle 圆点，越界回退 rect */
  shape?: 'rect' | 'stadium' | 'round' | 'diamond' | 'circle' | string
  sourceDoc?: string
  cite?: CiteChip
  status?: 'supported' | 'needs_human' | 'pending' | string
}

export interface FlowEdge {
  from: string
  to: string
  label?: string
  /** 虚线边（如待核实的关联、推测路径） */
  dashed?: boolean
  sourceDoc?: string
  cite?: CiteChip
  status?: 'supported' | 'needs_human' | 'pending' | string
}

export interface FlowSpec {
  title: string
  /** 布局方向：TB 自上而下 / LR 自左向右，缺省 TB */
  direction?: 'TB' | 'LR' | string
  nodes: FlowNode[]
  edges: FlowEdge[]
}

/** 可视化阅卷输出（时间线 + 人物关系 + 流程/证据关系图，本地组件渲染） */
export interface VizSpec {
  timeline: TimelineSpec
  relationGraph: RelationGraphSpec
  /** 流程类/证据关系图（可选；旧产物无此字段） */
  flows?: FlowSpec[]
  warnings?: string[]
}

/** 证据卡（PRD 13.5：材料记载字段与分析意见字段分开，REQ-EVI-001） */
export interface EvidenceCard {
  /** —— 材料记载字段 —— */
  name: string
  type: string
  sourceDoc?: string
  formedAt?: string
  provider?: string
  custody?: string
  purpose?: string
  keyFragment?: string
  /** —— 分析意见字段（与材料记载分开标注） —— */
  authenticity?: string
  legality?: string
  relevance?: string
  weight?: string
  relations: Array<{ to: string; relation: string }>
  defects?: string
  reinforce?: string
  crossExam?: string
  /** —— 校验状态 —— */
  cite?: CiteChip
  /** REQ-EVI-004：合法性无法判断（取得程序材料缺失）→ 待核实 */
  legalityPending?: boolean
}

/** 争点证据矩阵（REQ-EVI-003：支持/反对/冲突/缺失四种关系） */
export interface EvidenceMatrixSpec {
  disputes: string[]
  cells: Array<{
    dispute: string
    evidence: string
    relation: 'supports' | 'opposes' | 'conflict' | 'missing'
    note?: string
  }>
}

/** 证据分析输出（证据卡 + 争点证据矩阵） */
export interface EvidenceSpec {
  evidence: EvidenceCard[]
  matrix: EvidenceMatrixSpec
  warnings?: string[]
}

/** 策略方案（PRD 13.6：前提/依据/行动/收益/风险/对方回应） */
export interface StrategyPlan {
  title: string
  premises: string[]
  legalBasis: string[]
  actions: string[]
  benefits?: string
  risks?: string
  counter?: string
  sourceDoc?: string
  cite?: CiteChip
}

/** 辩护策略输出（REQ-STR-002~005） */
export interface StrategySpec {
  position: string
  keyDisputes: string[]
  favorable: Array<{ text: string; sourceDoc?: string; cite?: CiteChip }>
  unfavorable: Array<{ text: string; sourceDoc?: string; cite?: CiteChip }>
  infoGaps: string[]
  plans: StrategyPlan[]
  singlePlanReason: string
  evidenceActions: string[]
  userDecisions: string[]
  warnings?: string[]
}

/** 合同审查风险项（PRD 14.4 输出字段；contract.get_review 返回） */
export interface ContractRiskItem {
  id: string
  reviewId: string
  /** 引证 Claim（证据链可回溯） */
  claimId: string | null
  clauseId: string | null
  riskLevel: 'severe' | 'high' | 'medium' | 'low' | string
  riskLevelLabel: string
  category: 'business' | 'legal' | 'completeness' | string
  clauseRef: string
  originalText: string
  riskNote: string
  impact: string | null
  suggestion: string | null
  replacementText: string | null
  basis: string | null
  confidence: 'verified' | 'high' | 'medium' | 'low' | string
  decision: 'pending' | 'accepted' | 'rejected' | 'edited' | string
  editedText: string | null
  decidedAt: string | null
  clauseHeading?: string | null
  clausePage?: number | null
}

/** 合同审查会话元数据（contract_reviews 行投影） */
export interface ContractReviewMeta {
  id: string
  caseId: string
  taskId: string | null
  primaryDocumentId: string
  primaryVersionId: string
  primaryDocumentTitle: string | null
  reviewerRole: string
  reviewerRoleLabel: string
  customRole: string | null
  riskAppetite: string
  contractType: string | null
  transactionGoal: string | null
  governingLaw: string | null
  status: 'analyzing' | 'completed' | 'failed' | string
  degradedReason: string | null
  riskStats: {
    total: number
    severe: number
    accepted: number
    rejected: number
    pending: number
  }
  createdAt: string
  updatedAt: string
}

/** 合同工作副本版本（REQ-CON-006/002：只写独立 version，可比较、原件不变） */
export interface ContractWorkCopy {
  reviewId: string
  versionNo: number
  documentVersionId: string
  baseVersionId: string
  sha256: string
  sizeBytes: number
  appliedRiskIds: string[]
  createdAt: string
}

/** 合同审查完整载荷（contract.get_review） */
export interface ContractReviewPayload {
  review: ContractReviewMeta
  risks: ContractRiskItem[]
  clauses: Array<{ id: string; clauseRef: string; heading: string | null; pageNo: number | null }>
  definitions: Array<{ term: string; definition: string }>
  workCopy: ContractWorkCopy | null
}

/** 合同审查发起输入（PRD 14.2 输入表：主合同/审查方/风险偏好必填） */
export interface ContractStartInput {
  primaryDocumentId: string
  reviewerRole: string
  customRole?: string | null
  riskAppetite?: 'conservative' | 'balanced' | 'aggressive' | string
  contractType?: string | null
  transactionGoal?: string | null
  governingLaw?: string | null
  /** 其余输入原样随通道入库（如交易对方名称） */
  [key: string]: unknown
}

/** 消息流内的合同审查结果体（node_verify body.contract） */
export interface ContractBody {
  reviewId: string | null
  contractType: string | null
  risks: Array<
    Pick<
      ContractRiskItem,
      | 'riskLevel'
      | 'category'
      | 'clauseRef'
      | 'originalText'
      | 'riskNote'
      | 'impact'
      | 'suggestion'
      | 'replacementText'
      | 'basis'
      | 'confidence'
      | 'claimId'
    > & { cite?: CiteChip }
  >
  riskStats: { severe: number; high: number; medium: number; low: number; total: number; pending: number }
}

// ---------------------------------------------------------------------------
// 文书生成（DEV-MOD-007/008 / PRD §15、§16.3；与 Core docgen.* RPC 对齐）
// ---------------------------------------------------------------------------

/** 文书模板字段（模板变量解析结果；docgen.update_template_fields 输入项） */
export interface DocGenTemplateField {
  name: string
  required: boolean
}

/** 文书模板（docgen.list_templates / import_template / update_template_fields 返回） */
export interface DocGenTemplate {
  id: string
  caseId: string | null
  /** 同一模板版本组标识（首个版本的 id；版本列表/回退按组定位，REQ-TPL-005） */
  rootId: string
  title: string
  docType: string
  /** strict 锁定模板固定文字 / reference 仅作参照 */
  templateMode: string
  contentMd: string
  fields: DocGenTemplateField[]
  sourceName: string | null
  sourceSha256: string | null
  /** parse_failed 时保留原文，parseError 含行号，不可用于生成（REQ-TPL-004） */
  status: 'active' | 'disabled' | 'parse_failed' | string
  parseError?: string | null
  versionNo: number
  /** 组内当前版本指针（回退=移动指针，较新版本保留不删除，REQ-TPL-005） */
  isCurrent: boolean
  /** 模板来源格式（升级方案 §6.4）：md 文本 / txt / docx 二进制原件 */
  sourceFormat: 'md' | 'txt' | 'docx' | string
  /** DOCX 模板原件在内容存储中的只读位置（不返回本地绝对路径） */
  sourceBlobUri?: string | null
  sourceSizeBytes?: number | null
  /** 变量在段落/表格单元格/页眉页脚中的位置映射（strict 替换与定位提示用） */
  fieldMapping: Array<{ field: string; part: string; kind: string; index: number }>
  /** 格式警告：文本框/域代码/修订记录/内容控件等无法安全改写的内容 */
  formatWarnings: string[]
  createdAt: string
  updatedAt: string
}

/** DOCX 模板导入输入（docgen.import_docx_template，升级方案 §6.1） */
export interface DocGenDocxTemplateImportInput {
  caseId?: string | null
  title: string
  docType: string
  /** 模板文件内容（Main 进程读取文件后以 base64 传入，路径不进 Core 状态） */
  fileBase64: string
  sourceName?: string | null
  /** 不传则由解析结果决定：含显式变量 → strict，否则 → reference */
  templateMode?: 'strict' | 'reference' | null
  /** 更新既有模板版本组（原件哈希与字节写入新版本，历史草稿仍可回溯旧版本） */
  baseTemplateId?: string | null
}

/** DOCX 模板导入结果：解析出的字段与格式警告（前端在模板卡上直接可见） */
export interface DocGenDocxTemplateImportResult {
  template: DocGenTemplate
  /** 未检测到显式变量时按参考范本处理的提示 */
  warnings: string[]
}

/** 文书模板导入输入（docgen.import_template；baseTemplateId 给定时为该模板新增版本） */
export interface DocGenTemplateImportInput {
  caseId?: string | null
  title: string
  docType: string
  templateMd: string
  sourceName?: string | null
  /** 更新指定模板：解析成功则成为其版本组新当前版本（失败不入组，REQ-TPL-005） */
  baseTemplateId?: string | null
}

/** 文书模板版本条目（docgen.list_template_versions；历史版本按 version_no 回溯） */
export interface DocGenTemplateVersion {
  id: string
  rootId: string
  versionNo: number
  title: string
  docType: string
  templateMode: string
  status: 'active' | 'disabled' | 'parse_failed' | string
  parseError?: string | null
  sourceName: string | null
  sourceSha256: string | null
  charCount: number
  fields: DocGenTemplateField[]
  isCurrent: boolean
  createdAt: string
  updatedAt: string
}

/** 模板版本比较结果（docgen.diff_template_versions；差异结构同草稿版本比较） */
export interface DocGenTemplateDiffResult extends TextDiffResult {
  fromLabel: string
  toLabel: string
}

/** 文书生成发起输入（agent.start docgenInput 通道，PRD 15.2 输入表） */
export interface DocGenStartInput {
  /** 已选模板；缺省时由 Core 结合 docType 与关联案件自行起草 */
  templateId?: string | null
  docType?: string
  /** 起草目标或修改要求；缺省时 Core 依据模板与关联案件生成内部任务描述 */
  purpose?: string
  /** 模板类型与文书类型不匹配时，用户确认仍要使用后重发（PRD 15.6） */
  confirmMismatch?: boolean
  /**
   * 修订分叉（交付物迭代改写）：给定 revisionDraftId 时不建新草稿，
   * 以既有草稿当前稿为基准按 revisionInstruction AI 定向改写，
   * 落 kind='ai_revise' 版本（历史版本保留，可在版本列表回退）
   */
  revisionDraftId?: string
  /** 修订意见（revisionDraftId 给定时必填：用户本次的修改要求与建议） */
  revisionInstruction?: string
}

/** 槽位来源三分类（REQ-GEN-004） */
export type DocGenSlotSourceType = 'case_evidence' | 'user_confirmed' | 'pending_verify'

/** 槽位引证（sourceType=case_evidence 必填，正文内容可回溯案件原文） */
export interface DocGenSlotSourceRef {
  documentId: string
  documentTitle?: string | null
  pageNo?: number | null
  quote?: string | null
}

/** 提纲条目（points 为要点描述文本；修改提纲会复位确认态与正文，REQ-GEN-005） */
export interface DocGenOutlineItem {
  heading: string
  points: string
  sourceDoc?: string | null
}

/** 槽位填充值输入（docgen.fill_slots；value 空串表示清空该槽位） */
export interface DocGenSlotValueInput {
  name: string
  value: string
  sourceType: DocGenSlotSourceType | string
  sourceRef?: DocGenSlotSourceRef | null
}

/** 草稿槽位（Draft.slots 的值对象） */
export interface DocGenDraftSlot {
  value: string
  sourceType: DocGenSlotSourceType | string
  sourceRef?: DocGenSlotSourceRef | null
}

/** 阻断级占位符统计（正文「【待补充 字段名】」审计结果，REQ-GEN-007） */
export interface DocGenBlockingItem {
  name: string
  count: number
}

/** 结构化预览章节键（工作台/对话卡固定渲染顺序，Core 确定性派生） */
export type DocGenPreviewSectionKey = 'parties' | 'cause' | 'claims' | 'facts' | 'evidence' | 'tail'

/** 结构化预览章节（filled 展示值；pending 显式待补充，不编造） */
export interface DocGenPreviewSection {
  key: DocGenPreviewSectionKey | string
  label: string
  value: string | null
  status: 'filled' | 'pending'
  sourceType?: DocGenSlotSourceType | string
  sourceRef?: DocGenSlotSourceRef | null
  /** 该段由哪些草稿槽位组成（工作台点击可定位编辑） */
  slotNames?: string[]
}

/** 文书排版规范（A4 与法律文书常用版式；导出 Word 时按此应用） */
export interface DocGenLayout {
  paper: 'A4'
  paperSizeMm: { width: number; height: number }
  orientation: 'portrait'
  marginsCm: { top: number; bottom: number; left: number; right: number }
  titleStyle: { fontFamily: string; fontSize: string; bold: boolean; align: 'center' }
  bodyStyle: { fontFamily: string; fontSize: string; lineHeight: string }
}

/** 文书结构化预览（docgen.get_draft 返回；旧 Core 无此字段时前端降级） */
export interface DocGenPreview {
  title: string
  sections: DocGenPreviewSection[]
  layout: DocGenLayout
  usageTips: string[]
  missingFields: string[]
  blockingCount: number
}

/** 文书草稿（docgen.get_draft / 各操作返回；list_drafts 不含 bodyMd） */
export interface DocGenDraft {
  id: string
  caseId: string
  taskId: string | null
  templateId: string | null
  templateTitle: string | null
  templateVersionNo: number | null
  /** 生成所用模板内容哈希（G-002：历史产物保存模板版本与哈希，旧数据为 null） */
  templateContentSha256: string | null
  docType: string
  purpose: string
  outline: DocGenOutlineItem[] | null
  outlineConfirmed: boolean
  slots: Record<string, DocGenDraftSlot>
  bodyMd?: string
  /** outline 提纲待确认 / draft 已生成正文 / confirmed 已定稿 */
  status: 'outline' | 'draft' | 'confirmed' | string
  blocking: DocGenBlockingItem[]
  blockingCount: number
  /** 当前稿版本号（无版本为 0）：消息/文件卡/预览/修订的版本锚点（升级方案 §7.4） */
  currentVersionNo: number
  /** 结构化预览（随 get_draft 下发；旧 Core 缺省） */
  preview?: DocGenPreview
  createdAt: string
  updatedAt: string
}

/** 草稿历史版本（docgen.list_versions，REQ-GEN-010；DEV-MOD-009 版本比较） */
export interface DocGenDraftVersion {
  versionNo: number
  bodyMd: string
  /** render 渲染 / edit 手动编辑 / autosave 自动保存恢复点 / restore 恢复历史版本 / ai_revise AI 修订 */
  kind: 'render' | 'edit' | 'autosave' | 'restore' | 'ai_revise' | string
  kindLabel: string
  charCount: number
  blocking: DocGenBlockingItem[]
  createdAt: string
}

/** 正文保存结果（docgen.save_body，DEV-MOD-009 / REQ-GEN-009） */
export interface DocGenSaveResult {
  draft: DocGenDraft
  versionNo: number
  kind: 'edit' | 'autosave' | string
  /** 自动保存是否合并进同一恢复点（30 秒窗口内） */
  merged: boolean
  savedAt: string
}

/** AI 修订落库结果（docgen.revise；kind 固定 ai_revise，修订使定稿回到草稿态） */
export interface DocGenReviseResult {
  draft: DocGenDraft
  versionNo: number
  /** 本次修订基于的版本号（版本一致性锚点，升级方案 §7.4） */
  baseVersionNo: number
  kind: 'ai_revise' | string
  /** 本次修订意见（用户输入的修改要求，回显供版本列表展示） */
  instruction: string
  savedAt: string
}

/** 版本比较差异块（行级 diff；左右两侧均含上下文行，可直接并排渲染） */
export interface TextDiffHunk {
  type: 'insert' | 'delete' | 'replace'
  leftStart: number
  rightStart: number
  leftLines: string[]
  rightLines: string[]
}

/** 版本比较结果（docgen.diff_versions / contract.diff_work_copies，REQ-GEN-010、REQ-CON-002） */
export interface TextDiffResult {
  fromLabel: string
  toLabel: string
  stats: { added: number; removed: number }
  hunks: TextDiffHunk[]
  truncated: boolean
}

/** 编辑会话（editor.open；closed_at 缺失表示上次未正常退出，用于崩溃恢复） */
export interface EditorSession {
  id: string
  targetKind: string
  targetId: string
  caseId: string
  openedAt: string
  lastAutosaveAt?: string | null
  /** true = 复用崩溃残留会话，当前存在未提交的自动保存（界面须先提示恢复/放弃） */
  pending: boolean
}

/** 崩溃恢复项（editor.pending；存在未提交自动保存的未正常退出编辑，REQ-GEN-009） */
export interface EditorRecoveryItem {
  sessionId: string
  targetKind: string
  targetId: string
  caseId: string
  title: string
  docType: string | null
  openedAt: string
  lastAutosaveAt: string | null
  autosaveVersionNo: number | null
  lastManualVersionNo: number | null
}

/** 崩溃恢复处理结果（editor.resolve：recover 保留自动保存 / discard 回到手动版本） */
export interface EditorRecoveryResult {
  sessionId: string
  action: 'recover' | 'discard'
  draft: DocGenDraft | null
}

/** 文书导出结果（docgen.export；草稿态导出文件名带「草稿_」前缀） */
export interface DocGenExportResult {
  path: string
  format: 'md' | 'txt' | 'docx' | 'pdf' | 'xlsx' | 'pptx' | string
  draftMarked: boolean
  blockingCount: number
  /** 导出来源版本号（本次导出对应的草稿版本，审计可追溯；旧 Core 为 null） */
  versionNo: number | null
}

/** 修订分叉结果体（node_verify body.docgen.revision） */
export interface DocGenRevisionBody {
  /** 是否已落 ai_revise 新版本（本地降级/修订稿与当前稿一致时为 false） */
  applied: boolean
  /** 本次修订意见 */
  instruction: string
  /** 落库的新版本号（applied=true 时存在） */
  versionNo?: number
  /** 本次修订所基于的版本号（版本锚点：修订基于哪一版当前稿，升级方案 §7.4） */
  baseVersionNo?: number
  /** 模型给出的修订说明（改了哪些位置、为何这样改） */
  notes?: string
  /** applied=false 的原因（local_mode = 本地模式未执行语义修订；version_conflict = 基于旧稿被拒绝） */
  reason?: 'local_mode' | 'version_conflict' | string
}

/** 消息流内的文书生成结果体（node_verify body.docgen） */
export interface DocGenBody {
  draftId: string
  docType: string | null
  outline: DocGenOutlineItem[] | null
  outlineConfirmed: boolean
  /** 消息对应的草稿版本号（版本锚点：文件卡/摘要与当前稿一致性，升级方案 §7.4） */
  versionNo?: number
  slots: Array<{
    name: string
    value: string | null
    sourceType: DocGenSlotSourceType | string
    sourceTypeLabel?: string | null
    sourceRef?: DocGenSlotSourceRef | null
    claimId?: string | null
  }>
  missingFields: string[]
  /** 修订分叉（交付物迭代改写）：agent 修订任务的结果 */
  revision?: DocGenRevisionBody
}

/** AI 分析结果体：法律阅卷 = 全规格报告；其余模式 = 精简要点 */
export interface AiMessageBody {
  intro?: string
  points?: Array<{ title: string; desc: string }>
  report?: ReviewReport
  viz?: VizSpec
  evidence?: EvidenceSpec
  strategy?: StrategySpec
  contract?: ContractBody
  docgen?: DocGenBody
  /** chat 产物（自主循环收尾）：对话分析全文 */
  answer?: string
  /** 文件类产物（kind=file）：落盘文件名与字节大小（无路径） */
  fileName?: string
  sizeBytes?: number
  cites: CiteChip[]
  audit: string
  degradedReason?: string | null
}

/** 阅卷产物元数据（artifact.list，M2 收尾：产物概览数据源）。
 * 版本组语义：每组只返回组内最新版本（当前稿）。 */
export interface ArtifactMeta {
  id: string
  taskId: string
  caseId: string
  kind: 'report' | 'viz' | 'evidence' | 'strategy' | 'points' | string
  title: string
  createdAt: string
  /** 版本组标识（同组即同一交付物的历史版本；组内最新为当前稿） */
  groupId: string
  /** 组内版本号（当前稿 = 组内最大版本号） */
  versionNo: number
  /** 组内版本总数（含历史） */
  versionCount: number
  /** 文件类产物（kind=file）的落盘大小（字节）；旧数据无此字段 */
  sizeBytes?: number
  /** 文书生成落盘的 Word 草稿：点击文件卡打开文书工作台（预览/编辑/AI 修订） */
  docgenDraftId?: string
  /** 该文件对应的草稿版本号（版本锚点：与消息卡、工作台、版本历史同一编号） */
  docgenVersionNo?: number
  /** 文书草稿标识（未定稿或存在占位符时为 true，文件名带「草稿_」前缀） */
  draftMarked?: boolean
}

/** 产物完整内容（artifact.get） */
export interface ArtifactFull extends ArtifactMeta {
  body: AiMessageBody
}

/** 产物版本组历史条目（artifact.list_versions；传组内任意版本 id 均按组返回） */
export interface ArtifactVersionItem {
  id: string
  versionNo: number
  kind: string
  title: string
  taskId: string | null
  createdAt: string
}

/** 文件类产物直接编辑（AI 生成文档：docx 走 Word 模型，xlsx/pptx 走单元格/文本框，其他文本格式走源码） */
export interface ArtifactEditable {
  artifactId: string
  title: string
  kind: 'word' | 'text' | 'sheet' | 'slide'
  /** kind=word：Word 节点模型（docx 原件解析；复杂版式已按简化规则收敛） */
  model?: WordNode
  /** kind=text：Markdown / HTML / 纯文本源码 */
  text?: string
  /** kind=sheet：工作表可编辑网格（仅前 500 行 × 40 列） */
  sheets?: SheetEditSheet[]
  /** kind=slide：每页文本框 */
  slides?: SlideEditSlide[]
  /** 转换告警（如复杂版式、图片、页眉页脚未保留） */
  warnings?: string[]
  /** 保存可能丢失图表/图片（需显式确认） */
  risky?: boolean
}

export interface SheetEditSheet {
  name: string
  /** 起始行列（0-based，当前恒为 0；用于把 UI 相对位置换算回工作表坐标） */
  startRow: number
  startCol: number
  rowCount: number
  colCount: number
  /** 显示值（公式显示为 =...） */
  rows: string[][]
  merges?: Array<{ row: number; col: number; rowSpan: number; colSpan: number }>
  freezeRows?: number
  freezeCols?: number
}

export interface SheetCellChange {
  sheet: string
  /** 0-based 工作表绝对行列 */
  row: number
  col: number
  value: string
}

export interface SlideTextBox {
  shapeId: number
  name: string
  text: string
}

export interface SlideEditSlide {
  index: number
  title: string
  boxes: SlideTextBox[]
}

export interface SlideTextChange {
  slideIndex: number
  shapeId: number
  text: string
}

export interface ArtifactEditSaveResult {
  ok: boolean
  sizeBytes: number
  versionNo?: number | null
  warnings?: string[]
}

/** 导入的 Excel/PPT 原件创建的可编辑副本（登记为文件产物，原件只读） */
export interface EditableCopyResult {
  artifactId: string
  title: string
}

/** 产物可导出格式（均为原生文件；同格式导出为源文件字节复制） */
export type ArtifactExportFormat =
  | 'markdown'
  | 'docx'
  | 'pdf'
  | 'xlsx'
  | 'pptx'
  | 'html'
  | 'txt'
  | 'csv'
  | 'json'

/** 产物导出结果（null = 用户取消保存对话框；路径不进 Renderer） */
export interface ArtifactExportResult {
  ok: boolean
  fileName: string
  sizeBytes: number
  /** 导出来源版本号（本次导出对应的组内版本，审计可追溯；旧 Core 无此字段） */
  sourceVersionNo?: number
}

/** 可视化产物的图形导出结果（SVG/PNG；路径不进 Renderer） */
export interface ArtifactGraphicExportResult {
  ok: boolean
  fileName: string
  sizeBytes: number
}

/** 证据卷宗装订结果（路径不进 Renderer；failures 必须逐条可见） */
export interface EvidenceBundleItem {
  documentId: string
  title: string
  /** 卷内起始页码（1 基，含封面与目录） */
  startPage: number
  endPage: number
  /** 非 PDF 原件：已转换为 PDF 装入 */
  converted: boolean
}

export interface EvidenceBundleFailure {
  documentId: string
  title: string
  reason: string
}

export interface EvidenceBundleResult {
  ok: boolean
  fileName: string
  sizeBytes: number
  pageCount: number
  items: EvidenceBundleItem[]
  failures: EvidenceBundleFailure[]
}

/** SourceRef —— 引证定位对象，对应技术方案 9.1 */
export interface SourceRef {
  sourceRefId: string
  sourceType: 'case_document' | 'knowledge' | 'external_authority' | 'user_confirmed'
  documentId: string
  documentVersionId: string
  fileSha256: string
  pageNo: number | null
  printedPage: string | null
  blockIds: string[]
  bbox: [number, number, number, number] | null
  charStart: number | null
  charEnd: number | null
  quote: string
  blockHash: string
  corpusVersion: number
  ocrConfidence: number | null
}

/** Claim —— 结论与来源的绑定，对应技术方案 9.2 */
export interface Claim {
  claimId: string
  type: ClaimType
  text: string
  sourceRefIds: string[]
  status: ClaimStatus
  reasonCode: string | null
  confidenceBasis: 'exact_quote' | 'multiple_sources' | 'user_confirmed' | null
}

/** 待核实队列条目（claim.list 返回，PRD 11.4） */
export interface ClaimSummary {
  id: string
  taskId: string
  caseId: string
  type: string
  text: string
  status: ClaimStatus | string
  reasonCode: string | null
  confidenceBasis: string | null
  sources: string[]
  createdAt: string
  /** 矛盾证据（R-004）：conflict 状态时指向对侧 Claim */
  conflictsWith?: string | null
  counterpartText?: string | null
}

// ---------------------------------------------------------------------------
// Agent 任务与事件（对应技术方案 4.1 / 4.2 / 15）
// ---------------------------------------------------------------------------

export interface ClientPosition {
  role: string
  goals: string[]
  mustNotAssume: string[]
  userConfirmedFacts: string[]
  showOpposing: boolean
}

/** 跨任务对话历史一轮（仅 user/assistant 文本；Core 截断入库，支撑多轮流程续接） */
export interface ChatHistoryTurn {
  role: 'user' | 'assistant'
  text: string
}

export interface StartAgentRequest {
  caseId: string
  mode: AgentMode
  input: string
  selectedDocumentIds: string[]
  clientPosition: ClientPosition | null
  workflowPreset: string | null
  modelPolicy: { modelId: string }
  toolPolicy: { enabledTools: string[] }
  /** 本次任务绑定技能 id（/ 菜单选中） */
  skills?: string[]
  /** 最近几轮对话文本（多轮安装等流程续接上下文） */
  history?: ChatHistoryTurn[]
  /** 输入坞随问附件（临时性补充材料，不导入案件文档） */
  attachments?: ChatAttachment[]
}

export type ApprovalDecision = 'allow_once' | 'allow_task' | 'allow_case' | 'deny'

export interface ApprovalResponse {
  approvalId: string
  decision: ApprovalDecision
  approvedPayloadHash: string
}

export interface TaskEvent {
  taskId: string
  seq: number
  eventType: string
  payload: Record<string, unknown>
  createdAt: string
}

// ---------------------------------------------------------------------------
// 脱敏（M3 / 技术方案 11.3 / D-001~D-011）
// ---------------------------------------------------------------------------

/** redaction.policy 返回：档位 + 当前生效的类型集合 */
export interface RedactionPolicyInfo {
  policy: RedactionPolicy
  types: string[]
}

/** detect 命中明细（纯检测，不建映射不落库） */
export interface RedactionHit {
  type: string
  typeLabel: string
  original: string
  start: number
  end: number
  confidence: number
  proposedStatus: RedactionEntityStatus
}

/** preview 差异项（本机受信 UI 场景，含原文 → 别名对照） */
export interface RedactionPreviewItem {
  entityId: string
  type: string
  typeLabel: string
  original: string
  alias: string
  confidence: number
  status: RedactionEntityStatus
  /** 同一实体在文本中的第几次出现（从 1 起） */
  occurrence: number
}

/** redaction.preview 返回（REQ-PRIV-005 外发差异预览） */
export interface RedactionPreview {
  caseId: string
  policy: RedactionPolicy
  totalHits: number
  items: RedactionPreviewItem[]
  needsHumanCount: number
  blockedCount: number
  /** 脱敏样例（截取前 400 字符，不含未确认高敏感实体原文） */
  sampleRedactedText: string
}

/** redaction.apply 返回（GOAL-004：先脱敏后授权） */
export interface RedactionApplyResult {
  caseId: string
  policy: RedactionPolicy
  /** 脱敏后文本（不含任何原文） */
  redactedText: string
  replacedCount: number
  entities: Array<{
    id: string
    type: string
    alias: string
    status: RedactionEntityStatus
  }>
  needsHumanIds: string[]
  /** 未确认高敏感实体 id；非空时调用方必须阻断外发（REQ-PRIV-006） */
  blockedBy: string[]
}

/** 案件实体映射行（list_entities；本机受信 UI，含解密原文供差异核对） */
export interface RedactionEntity {
  id: string
  caseId: string
  type: string
  typeLabel: string
  alias: string
  confidence: number
  status: RedactionEntityStatus
  createdAt: string
  /** 解密原文，仅本机受信场景返回 */
  original?: string
}

/** confirm_entities 返回 */
export interface RedactionConfirmResult {
  ok: boolean
  action: 'confirm' | 'reject'
  count: number
}

/** 案件自定义敏感词（strict / custom 档勾选 custom 类型时生效） */
export interface RedactionTerm {
  id: string
  caseId: string
  term: string
  createdAt: string
}

/**
 * 技能（REQ-AI-003 / REQ-AI-007）：提示词 + 允许工具白名单的能力包，
 * 由 Agent 经 skill_install 从本地路径安装（非可执行代码）。
 */
export interface SkillInfo {
  id: string
  name: string
  description: string
  sourceUrl: string
  /** 技能提示词正文（安装时不回传给 UI 也可；list 全量返回供详情展示） */
  promptMd: string
  /** 允许工具白名单（内置领域工具名子集） */
  allowedTools: string[]
  enabled: boolean
  createdAt: string
}

/** 记忆条目（DEV-AGT-007 / M-001~M-006；四层中知识库层当前范围外） */
export interface MemoryEntry {
  id: string
  scope: 'session' | 'case' | 'user'
  caseId?: string | null
  taskId?: string | null
  kind: string
  key: string
  value: string
  /** 来源（M-002：文档、版本、页码与原文片段；无来源为 null） */
  source?: {
    documentId: string
    versionId?: string | null
    pageNo?: number | null
    quote?: string | null
  } | null
  status: 'active' | 'stale' | 'superseded' | 'deleted'
  supersededBy?: string | null
  createdAt: string
  updatedAt: string
}

/** 加密备份结果（DEV-FND-006 / DB-006：路径由 Main 持有，Renderer 只见文件名级信息） */
export interface BackupResult {
  dbPath: string
  keyFilePath: string
  manifestPath: string
  manifest: {
    createdAt: string
    schemaVersion: number
    dbSizeBytes: number
    counts: Record<string, number>
    audit: { total: number; tailHash: string | null }
    hashSamples: number
  }
  /** 未提供口令时的一次性恢复密钥；仅在本次返回，需离线保存 */
  recoveryKey: string | null
}

/** 备份校验结果（DB-006：逐项一致性） */
export interface BackupVerifyResult {
  ok: boolean
  checks: Array<{ name: string; ok: boolean; value: unknown }>
  counts: Record<string, number>
  schemaVersion: number
}

/** 页文本更正记录（O-008：OCR 原值与更正值并存，追加式，原件与页图不变） */
export interface OcrCorrection {
  id: string
  originalText: string
  correctedText: string
  originalConfidence?: number | null
  note?: string | null
  createdAt: string
}

/** 页文本更正结果（Q-009：引语不再可定位的 Claim 转待校验，不静默错位） */
export interface OcrCorrectionResult {
  documentId: string
  pageNo: number
  correctionId: string
  staleClaims: number
}

/** 批注荧光标注色（红/黄/蓝荧光笔）；null 表示历史数据默认黄 */
export type AnnotationColor = 'red' | 'yellow' | 'blue'

/** 页批注（DEV-DOC-010：派生数据，原件只读；quote 为可选原文锚定片段） */
export interface DocumentAnnotation {
  id: string
  documentId: string
  versionId: string
  pageNo: number
  quote: string | null
  /** 页文本变化后 quote 不再可定位（Q-009 不静默错位；读取时计算） */
  quoteMissing: boolean
  body: string
  /** 荧光标注色；历史数据为 null（按默认黄展示） */
  color: AnnotationColor | null
  status: 'open' | 'resolved'
  resolvedAt: string | null
  createdAt: string
  updatedAt: string
}

/** 产物批注（AI 生成产物的派生数据；产物无页模型，整篇单锚并绑定具体产物版本） */
export interface ArtifactAnnotation {
  id: string
  artifactId: string
  quote: string | null
  /** 形状与 DocumentAnnotation 对齐；产物整篇渲染由前端正文匹配，恒为 false */
  quoteMissing: boolean
  body: string
  color: AnnotationColor | null
  status: 'open' | 'resolved'
  resolvedAt: string | null
  createdAt: string
  updatedAt: string
}

/**
 * 原件内容会话（REQ-DOC-014：按格式原样预览）。
 *
 * Renderer 不接触文件路径：`openContent` 由 Core 在案件临时目录物料化只读副本，
 * 返回不透明 contentId；`readContent` 分块取字节；`closeContent` 释放明文副本。
 * 原件本身始终只读，会话结束（或超时 / Core 退出）即清理。
 */
export interface DocumentContentInfo {
  contentId: string
  documentId: string
  kind: 'pdf' | 'docx' | 'xlsx' | 'pptx' | 'txt' | 'md' | 'html' | 'png' | 'jpg' | 'tiff' | string
  mime: string
  sizeBytes: number
  /** 页级会话（TIFF 按帧转 PNG）时的页码，整份文件会话为 null */
  pageNo: number | null
  /** 是否来自已解锁的加密文档（REQ-DOC-004） */
  unlocked: boolean
}

/** 原件内容分块（0 起偏移；`eof` 表示已到末尾） */
export interface DocumentContentChunk {
  contentId: string
  offset: number
  dataBase64: string
  eof: boolean
}

// ---------------------------------------------------------------------------
// Renderer 可见 API 面（对应技术方案第 15 节）
// ---------------------------------------------------------------------------

export interface LawBuddyApi {
  workspace: {
    create(req: { name: string; rootUri: string }): Promise<WorkspaceInfo>
    open(req: { rootUri: string }): Promise<WorkspaceInfo>
    getSettings(): Promise<Record<string, unknown>>
  }
  case: {
    create(req: Partial<CaseInfo> & { title: string }): Promise<CaseInfo>
    list(): Promise<CaseInfo[]>
    get(req: { caseId: string }): Promise<CaseInfo>
    archive(req: { caseId: string }): Promise<void>
    restore(req: { caseId: string }): Promise<void>
  }
  document: {
    pickFiles(): Promise<{ importToken: string; names: string[] }>
    import(req: { caseId: string; importToken: string }): Promise<DocumentMeta[]>
    getMetadata(req: { documentId: string }): Promise<DocumentMeta>
    search(req: { caseId: string; query: string }): Promise<unknown[]>
    requestUnlock(req: { documentId: string }): Promise<void>
  }
  agent: {
    start(req: StartAgentRequest): Promise<{ taskId: string }>
    resume(req: { taskId: string }): Promise<void>
    cancel(req: { taskId: string }): Promise<void>
    respondToHuman(req: { taskId: string; answer: string }): Promise<void>
    /** 文书生成人机决策：提交问题集回答并恢复任务 */
    answerHumanQuestions(req: {
      taskId: string
      questionSetId: string
      answers: HumanAnswer[]
    }): Promise<void>
    /** 待回答问题集（刷新后恢复待答卡片） */
    getPendingQuestions(req: { taskId: string }): Promise<HumanQuestionSet | null>
    getSnapshot(req: { taskId: string }): Promise<Record<string, unknown>>
    subscribeEvents(req: { taskId: string; afterSeq?: number }): Promise<void>
  }
  approval: { respond(req: ApprovalResponse): Promise<void> }
  artifact: {
    list(req: { caseId: string }): Promise<ArtifactMeta[]>
    get(artifactId: string): Promise<ArtifactFull>
    export(req: { artifactId: string; format: ArtifactExportFormat; title: string }): Promise<ArtifactExportResult | null>
    dismiss(req: { artifactId: string }): Promise<ArtifactFull>
    annotations(artifactId: string): Promise<ArtifactAnnotation[]>
    addAnnotation(req: { artifactId: string; body: string; quote?: string; color?: string }): Promise<ArtifactAnnotation>
    updateAnnotation(req: { annotationId: string; body: string; color?: string }): Promise<ArtifactAnnotation>
    resolveAnnotation(req: { annotationId: string; resolved: boolean }): Promise<ArtifactAnnotation>
    deleteAnnotation(req: { annotationId: string }): Promise<{ deleted: boolean; annotationId: string }>
  }
  settings: {
    get(): Promise<Record<string, unknown>>
    update(req: Record<string, unknown>): Promise<void>
  }
}

// ---------------------------------------------------------------------------
// Preload 实际暴露的桥（本迭代实现窗口控制与 Core 状态；域数据走 LawBuddyApi mock）
// ---------------------------------------------------------------------------

export type CoreState = 'starting' | 'ready' | 'degraded' | 'stopped'

export interface CoreStatus {
  state: CoreState
  detail: string
  pythonVersion?: string
  langgraph?: boolean
  paddleocr?: boolean
  restartCount?: number
}

export interface LawBuddyBridge {
  /** 与 NodeJS.Platform 对齐，但 contracts 包不依赖 Node 类型 */
  platform: 'win32' | 'darwin' | 'linux' | (string & {})
  versions: { electron: string; chrome: string }
  window: {
    minimize(): void
    toggleMaximize(): void
    close(): void
  }
  core: {
    status(): Promise<CoreStatus>
    ping(): Promise<{ pong: boolean; latencyMs: number }>
    onStatus(cb: (s: CoreStatus) => void): () => void
  }
  /** 工作空间（本迭代 Main 转发 Core RPC） */
  workspace: {
    /** 打开上次使用的工作空间（缺失时回落到默认目录） */
    ensure(): Promise<WorkspaceInfo>
    /** 弹出系统目录选择框，返回用户选择的绝对路径；取消返回 null */
    pickDir(): Promise<string | null>
    /** 切换到指定目录的本地工作空间 */
    open(rootUri: string): Promise<WorkspaceInfo>
  }
  /** HTML 预览（Agent 可视化产出）：注册完整 HTML 文档到 Main 内存，返回 lawbuddy-preview://<id> 供沙箱 iframe 加载 */
  preview: {
    register(html: string): Promise<string>
    /** 按 id（或完整 lawbuddy-preview:// URL）取回预览 HTML；无内容返回 null（历史会话恢复源码页签用） */
    fetch(id: string): Promise<string | null>
    /**
     * 预览内容导出为图片：Renderer 在预览沙箱内序列化真实图形（SVG 文本 /
     * canvas 栅格化的 PNG base64），Main 弹保存对话框后落盘。
     */
    saveImage(req: {
      format: 'svg' | 'png'
      /** 保存对话框默认文件名 */
      title: string
      /** format=svg 时的 SVG 文本 */
      svg?: string
      /** format=png 时的 PNG base64 */
      dataBase64?: string
    }): Promise<ArtifactGraphicExportResult | null>
  }
  case: {
    create(req: CaseCreateInput): Promise<CaseInfo>
    copyMaterials(req: { fromCaseId: string; toCaseId: string; documentIds: string[]; clientRequestId: string; confirmed: boolean }): Promise<{
      fromCaseId: string; toCaseId: string
      mappings: Array<{ sourceDocumentId: string; sourceVersionId: string; documentId: string; versionId: string }>
    }>
    get(caseId: string): Promise<CaseInfo>
    list(includeArchived?: boolean): Promise<CaseInfo[]>
    archive(caseId: string): Promise<CaseInfo>
    restore(caseId: string): Promise<CaseInfo>
    /** 永久删除任务及其文档、索引、会话与引证数据 */
    delete(req: {
      caseId: string
    }): Promise<{
      deleted: boolean
      caseId: string
      title: string
      documents: number
      tasks: number
    }>
  }
  document: {
    /**
     * 打开系统选择器，返回一次性 import token（Renderer 不接触路径）。
     * mode='file' 选文件（默认）；mode='folder' 选文件夹并递归收集支持格式的文件，
     * 不支持的格式自动过滤；skipped 为被过滤数量；token 为空串表示无可导入文件。
     */
    pick(mode?: 'file' | 'folder'): Promise<{ token: string; names: string[]; skipped: number } | null>
    /**
     * 读取剪贴板图片（截图粘贴）作为随问附件返回（base64，不经案件导入与 OCR）；
     * 剪贴板中没有图片时返回 null。
     */
    readClipboardImage(): Promise<ChatAttachment | null>
    /**
     * 打开系统选择器挑选随问附件（图片与纯文本，多选），读取内容以 base64 返回；
     * 不支持的格式 / 超大文件自动跳过（skipped 为被跳过数量）；用户取消返回 null。
     */
    pickAttachments(): Promise<{ attachments: ChatAttachment[]; skipped: number } | null>
    /** 使用 token 完成导入；token 用后即焚（Main 自动分批，导入期间持续推送进度） */
    importByToken(req: { caseId: string; token: string }): Promise<ImportOutcome[]>
    /** 订阅批量导入进度（每批完成推送一次）；返回退订函数 */
    onImportProgress(cb: (p: DocumentImportProgress) => void): () => void
    list(caseId: string): Promise<DocumentMeta[]>
    getPages(documentId: string): Promise<DocumentPage[]>
    /** 页内 OCR 文本行与归一化坐标（扫描件 PDF/图片原件片段定位） */
    getTextBlocks(documentId: string, pageNo: number): Promise<DocumentTextBlock[]>
    /**
     * 原件内容会话（REQ-DOC-014）：按原始格式预览（PDF/Word/Excel/Markdown/HTML/图片）。
     * pageNo 仅 TIFF 等多帧图片需要（按帧转 PNG）；其余类型忽略。
     */
    openContent(req: { documentId: string; pageNo?: number }): Promise<DocumentContentInfo>
    /** 为导入的 Excel/PPT 原件创建可编辑副本（原件只读，副本登记为文件产物） */
    createEditableCopy(req: { documentId: string }): Promise<EditableCopyResult>
    /** 分块读取会话内容（Renderer 侧拼装 Blob，不落明文文件） */
    readContent(req: {
      contentId: string
      offset: number
      length?: number
    }): Promise<DocumentContentChunk>
    /** 关闭会话：释放案件临时目录中的明文副本 */
    closeContent(contentId: string): Promise<{ closed: boolean; contentId: string }>
    /**
     * 证据卷宗装订：把所选材料合并为带封面/目录/连续页码/书签的 PDF。
     * 目标路径由 Main 保存对话框持有（不进 Renderer）；null = 用户取消。
     */
    exportBundle(req: {
      caseId: string
      documentIds: string[]
    }): Promise<EvidenceBundleResult | null>
    /** OCR 人工更正（DEV-DOC-010 / O-008 / Q-009：原值与更正值并存，失效引用转待校验） */
    correctOcr(req: {
      documentId: string
      pageNo: number
      correctedText: string
      note?: string
    }): Promise<OcrCorrectionResult>
    pageCorrections(req: { documentId: string; pageNo: number }): Promise<OcrCorrection[]>
    /** 页批注（DEV-DOC-010）：quote_missing 读取时计算；归档案件拒绝写入 */
    annotations(req: { documentId: string }): Promise<DocumentAnnotation[]>
    addAnnotation(req: {
      documentId: string
      pageNo: number
      body: string
      quote?: string
      color?: AnnotationColor
    }): Promise<DocumentAnnotation>
    updateAnnotation(req: { annotationId: string; body: string; color?: AnnotationColor }): Promise<DocumentAnnotation>
    resolveAnnotation(req: { annotationId: string; resolved: boolean }): Promise<DocumentAnnotation>
    deleteAnnotation(req: { annotationId: string }): Promise<{ deleted: boolean; annotationId: string }>
    /** 删除单份文档（R-007：受影响 Claim 标记失效；M-003：相关记忆转失效） */
    delete(documentId: string): Promise<{
      deleted: boolean
      documentId: string
      staleClaims: number
      staleMemories: number
    }>
    /** 文档级脱敏预览：按类型返回命中计数（「脱敏处理」按钮确认框） */
    redactPreview(req: { caseId: string; documentId: string }): Promise<RedactPreviewResult>
    /** 生成派生脱敏版文档（原件不动；幂等） */
    redactApply(req: { documentId: string }): Promise<RedactApplyResult>
    /** 案件内全文检索（服务端强制 case_id 过滤，跨案召回为 0） */
    search(req: { caseId: string; query: string; limit?: number }): Promise<SearchHit[]>
    /**
     * 加密文档解锁（REQ-DOC-004/011）：密码经专用窗口输入，
     * 只经受控通道传给隔离解密进程，不入库、不进日志、不进入 Prompt。
     */
    requestUnlock(req: { documentId: string; password: string }): Promise<DocumentUnlockResult>
    /** OCR：导入图片文档后启动；状态轮询；暂停/恢复/取消 */
    ocr: {
      start(documentId: string): Promise<OcrTaskStatus>
      status(documentId: string): Promise<OcrTaskStatus>
      pause(documentId: string): Promise<OcrTaskStatus>
      resume(documentId: string): Promise<OcrTaskStatus>
      cancel(documentId: string): Promise<OcrTaskStatus>
    }
  }
  /** 审计（REQ-AUD-001） */
  audit: {
    verify(): Promise<AuditVerifyResult>
    list(req: { caseId?: string; action?: string; limit?: number }): Promise<AuditLogEntry[]>
  }
  /** Agent（DEV-AGT） */
  agent: {
    start(req: {
      caseId: string
      mode: AgentMode
      input: string
      workflowKind?: 'docgen_dialogue' | 'contract_dialogue'
      /** 法律阅卷工作流预设（PRD 13 四类功能入口） */
      workflowPreset?: 'review_analysis' | 'visualization' | 'evidence_analysis' | 'strategy'
      /**
       * 辩护策略立场（preset=strategy 时随任务生效，覆盖案件委托人身份）：
       * 输入明确（如「为被告生成辩护策略」）或经立场确认对话框选择后传入；缺省回退案件设置
       */
      strategyStance?: string
      /**
       * modelPolicy.redactionEnabled：本次任务外发云端模型前是否启用脱敏
       * （阅卷/合同审查发起前由用户选择；缺省按开启处理，安全兜底）
       */
      modelPolicy?: {
        modelId?: string
        remote?: boolean
        reasoningEffort?: ReasoningEffort
        redactionEnabled?: boolean
      }
      /** 合同审查输入（PRD 14.2 输入表；mode=contract_review 必填） */
      contractInput?: ContractStartInput | { overviewId: string }
      /** 文书生成输入（PRD 15.2 输入表；mode=document_generation 必填） */
      docgenInput?: DocGenStartInput | {
        templateId?: string
        promptDocument?: PromptDocument
        /** 自然语言起草：选中模板的文书类型与用户需求（Core 兜底建草稿用） */
        docType?: string
        purpose?: string
      }
      /** 本次任务绑定技能 id（/ 菜单选中；Core 校验存在且启用） */
      skills?: string[]
      /** 最近几轮对话文本（无会话时的兜底；有 conversationId 时 Core 用全文历史覆盖） */
      history?: ChatHistoryTurn[]
      /** 输入坞随问附件（截图/图片直发模型，不 OCR 不入案件文档；临时性补充材料） */
      attachments?: ChatAttachment[]
      /** 会话 id（V26）：同会话续聊时传入；缺省由 Core 新建并随返回值回传 */
      conversationId?: string
    }): Promise<AgentTaskInfo>
    getTask(taskId: string): Promise<AgentTaskInfo>
    resume(taskId: string): Promise<AgentTaskInfo>
    getEvents(req: { taskId: string; afterSeq?: number }): Promise<AgentTaskEvent[]>
    cancel(taskId: string): Promise<AgentTaskInfo>
    /** 回答文书生成的人机决策问题：校验后写入 user_confirmed 并从同一检查点恢复 */
    answerHumanQuestions(req: {
      taskId: string
      questionSetId: string
      answers: HumanAnswer[]
    }): Promise<AgentTaskInfo>
    /** 待回答的问题集（任务快照 / 刷新或重启后恢复待答卡片；无则 null） */
    getPendingQuestions(req: { taskId: string }): Promise<HumanQuestionSet | null>
    /** 订阅 Core 推送的任务事件（REQ-AI-001）；返回退订函数；重复事件按 seq 去重 */
    onAgentEvent(cb: (n: AgentEventNotification) => void): () => void
  }
  /** 设置（模型 API Key 等；敏感键只回传 configured 状态） */
  settings: {
    get(key: string): Promise<unknown>
    set(req: { key: string; value: unknown }): Promise<{ ok: boolean }>
  }
  /** 模型服务商（内置官方预设 + 自定义 API；方法与 Core model.* RPC 一一对应） */
  model: {
    /** 完整目录（服务商 / 模型能力 / 密钥 configured 状态；不含密钥明文） */
    catalog(): Promise<ModelCatalog>
    saveProvider(req: SaveProviderRequest): Promise<ModelProviderInfo>
    removeProvider(req: { providerId: string }): Promise<{ removed: boolean }>
    resetProvider(req: { providerId: string }): Promise<{ reset: boolean }>
    setProvider(req: { providerId: string }): Promise<{ ok: boolean }>
    fetchModels(req: {
      providerId: string
      /** 草稿协议（决定鉴权方式；缺省沿用已保存的服务商配置） */
      protocol?: ModelProtocol
      base?: string
      apiKey?: string
    }): Promise<FetchedModelInfo[]>
    testConnection(req: {
      providerId: string
      protocol?: ModelProtocol
      base?: string
      apiKey?: string
      /** 草稿里的模型 ID：提供时发一次最小对话请求，真正验证该模型可用 */
      model?: string
      /**
       * 是否验证图片输入（配置里勾了「图片」时传 true）：
       * 文本通过后再发一次带极小图片的请求，确认该模型真的接受图片。
       */
      checkVision?: boolean
    }): Promise<ModelTestResult>
  }
  /** 审批与权限（DEV-AGT-004） */
  approval: {
    respond(req: { approvalId: string; decision: 'allow_once' | 'allow_task' | 'allow_case' | 'deny' }): Promise<{
      approvalId: string
      status: string
    }>
  }
  /** 加密备份与恢复校验（DEV-FND-006 / DB-006；恢复需离线执行，见恢复脚本） */
  backup: {
    /** 在线创建加密备份（目录由 Main 弹选择框持有；口令经用户输入，不落日志） */
    create(req: { passphrase: string }): Promise<BackupResult | null>
    verify(req: { dbPath: string; keyFile: string; passphrase: string }): Promise<BackupVerifyResult>
  }
  /** 记忆管理（DEV-AGT-007：列表 / 召回 / 更正 / 删除；案件隔离由 Core 强制） */
  memory: {
    /** includeShared：同时返回关联案件（只读共享）的案件记忆 */
    list(req: { caseId?: string; scope?: string; status?: string; includeShared?: boolean }): Promise<MemoryEntry[]>
    recall(req: { caseId: string; query: string; limit?: number }): Promise<MemoryEntry[]>
    correct(req: { memoryId: string; value: string; note?: string }): Promise<{ id: string; supersededId: string; value: string }>
    forget(req: { memoryId: string; note?: string }): Promise<{ id: string; deleted: boolean }>
  }
  /** 策略确认（REQ-STR-005：用户选定方案写入审计，AI 不自动选择） */
  strategy: {
    decide(req: {
      taskId: string
      planTitle: string
      decision?: 'adopted' | 'rejected'
    }): Promise<{ ok: boolean; decision: string; planTitle: string }>
  }
  /** 合同审查（M3 / DEV-MOD-005/006；方法与 Core contract.* RPC 一一对应） */
  contract: {
    createOverview(caseId: string): Promise<ContractOverview>
    getOverview(overviewId: string): Promise<ContractOverview>
    updateOverview(req: { overviewId: string; config: Omit<ContractOverviewConfig, 'primaryVersionId'>; expectedRevision: number }): Promise<ContractOverview>
    getChecklist(req: { overviewId: string; snapshotId?: string }): Promise<ContractChecklistSnapshot | null>
    generateChecklist(req: { overviewId: string; expectedRevision: number; modelPolicy?: { remote?: boolean; modelId?: string; reasoningEffort?: ReasoningEffort } }): Promise<AgentTaskInfo>
    updateChecklist(req: { overviewId: string; items: ContractChecklistItem[]; expectedRevision: number }): Promise<ContractChecklistSnapshot>
    confirmChecklist(req: { overviewId: string; expectedRevision: number }): Promise<ContractChecklistSnapshot>
    startRiskReview(req: { overviewId: string; snapshotId: string; clientRequestId: string; modelPolicy?: { remote?: boolean; modelId?: string; reasoningEffort?: ReasoningEffort } }): Promise<AgentTaskInfo>
    getRiskRun(overviewId: string): Promise<ContractRiskRun | null>
    applyRiskSuggestion(req: { overviewId: string; riskId: string; targetId: string; expectedRevision: number; replacement: string; clientRequestId: string }): Promise<{ workCopy: WordWorkCopy; riskId: string; applyState: 'applied' }>
    listReviews(caseId: string): Promise<ContractReviewMeta[]>
    getReview(reviewId: string): Promise<ContractReviewPayload>
    /** 接受 / 拒绝 / 编辑（edit 必填 editedText）；返回决策后完整载荷 */
    decideRisk(req: { riskId: string; decision: 'accept' | 'reject' | 'edit'; editedText?: string }): Promise<ContractReviewPayload>
    undo(reviewId: string): Promise<ContractReviewPayload>
    redo(reviewId: string): Promise<ContractReviewPayload>
    /** 工作副本版本链（REQ-CON-002 版本比较） */
    listWorkCopies(reviewId: string): Promise<ContractWorkCopy[]>
    /** 版本比较：fromVersionNo=0 为原件基准；toVersionNo 缺省为最新工作副本 */
    diffWorkCopies(req: {
      reviewId: string
      fromVersionNo: number
      toVersionNo?: number | null
    }): Promise<TextDiffResult>
  }
  word: {
    openEditor(targetId: string): Promise<EditorSession>
    listWorkCopies(caseId: string): Promise<WordWorkCopy[]>
    createWorkCopy(req: { caseId: string; documentId?: string; draftId?: string; simplify?: boolean }): Promise<WordWorkCopy>
    getModel(targetId: string): Promise<WordWorkCopy>
    saveModel(req: { targetId: string; model: WordNode; expectedRevision: number; clientRequestId: string; kind: 'edit' | 'autosave'; sessionId?: string }): Promise<WordWorkCopy>
    listVersions(targetId: string): Promise<Array<{ revision: number; kind: string; createdAt: string }>>
    restoreVersion(req: { targetId: string; revision: number; expectedRevision: number }): Promise<WordWorkCopy>
    diffVersions(req: { targetId: string; fromRevision: number; toRevision: number }): Promise<TextDiffResult>
    applyPatches(req: { targetId: string; patches: Array<{ quote: string; replacement: string }>; expectedRevision: number; clientRequestId: string }): Promise<WordWorkCopy>
    /** 接受/拒绝修订（revisionIds 缺省处理全部），返回新修订版本 */
    resolveRevisions(req: { targetId: string; action: 'accept' | 'reject'; revisionIds?: string[]; expectedRevision: number; clientRequestId: string }): Promise<WordWorkCopy>
    export(targetId: string): Promise<{ revision: number; exported: boolean } | null>
  }
  /** 文书生成（DEV-MOD-007/008；方法与 Core docgen.* RPC 一一对应） */
  docgen: {
    installBestPractices(caseId: string): Promise<BestPracticeTemplate[]>
    listBestPractices(caseId: string): Promise<BestPracticeTemplate[]>
    /** 案件模板 + 全局模板 */
    listTemplates(caseId: string): Promise<DocGenTemplate[]>
    /** 导入模板：解析失败不抛错，返回 status=parse_failed 且 parseError 含行号 */
    importTemplate(req: DocGenTemplateImportInput): Promise<DocGenTemplate>
    /** 上传 .docx 范本作为自定义模板（升级方案 §6.1）：原件只读保存并解析变量/格式警告 */
    importDocxTemplate(req: DocGenDocxTemplateImportInput): Promise<DocGenDocxTemplateImportResult>
    /** 选择 .docx 模板文件（Main 弹文件选择，只回传文件名/大小/字节，不回传本地路径） */
    pickDocxTemplate(): Promise<{ name: string; sizeBytes: number; fileBase64: string } | null>
    updateTemplateFields(req: { templateId: string; fields: DocGenTemplateField[] }): Promise<DocGenTemplate>
    deleteTemplate(templateId: string): Promise<{ deleted: boolean; id: string }>
    /** 模板版本组历史（REQ-TPL-005）：传任意版本的 id 均按组返回全部版本 */
    listTemplateVersions(templateId: string): Promise<DocGenTemplateVersion[]>
    /** 模板版本比较：toVersionNo 缺省为组内当前版本（REQ-TPL-005） */
    diffTemplateVersions(req: {
      templateId: string
      fromVersionNo: number
      toVersionNo?: number | null
    }): Promise<DocGenTemplateDiffResult>
    /** 回退到指定版本：仅移动当前版本指针，较新版本保留不删除（REQ-TPL-005） */
    rollbackTemplate(req: { templateId: string; versionNo: number }): Promise<DocGenTemplate>
    /** 列表不含 bodyMd */
    listDrafts(caseId: string): Promise<DocGenDraft[]>
    getDraft(draftId: string): Promise<DocGenDraft>
    /** case_evidence 来源必须带 sourceRef（文档与页码），否则报错 */
    fillSlots(req: { draftId: string; values: DocGenSlotValueInput[] }): Promise<DocGenDraft>
    /** 修改提纲会复位确认态与正文 */
    updateOutline(req: { draftId: string; items: DocGenOutlineItem[] }): Promise<DocGenDraft>
    confirmOutline(draftId: string): Promise<DocGenDraft>
    /** 提纲未确认时报错 */
    render(draftId: string): Promise<DocGenDraft>
    /** 按 DOCX 模板原格式生成 Word（升级方案 §6.2）：strict 在原 OOXML 上替换变量，
     * reference 保留页面设置/页眉页脚重写正文；结果落新版本并返回格式警告 */
    renderDocx(req: { draftId: string; bodyMd?: string; allowUnconfirmed?: boolean }): Promise<{
      draft: DocGenDraft
      versionNo: number
      templateMode: 'strict' | 'reference' | string
      warnings: string[]
    }>
    /** 有阻断级占位符时报错 */
    confirm(draftId: string, review?: { contentReviewed: boolean; expectedUpdatedAt: string }): Promise<DocGenDraft>
    listVersions(draftId: string): Promise<DocGenDraftVersion[]>
    restoreVersion(req: { draftId: string; versionNo: number }): Promise<DocGenDraft>
    /** 正文编辑保存（DEV-MOD-009）：kind=edit 手动落版本；kind=autosave 停止输入 2 秒自动保存。
     * expectedVersionNo 为编辑开始时的当前稿版本号，与当前稿不一致时拒绝保存（防并发覆盖） */
    saveBody(req: {
      draftId: string
      bodyMd: string
      kind: 'edit' | 'autosave'
      sessionId?: string | null
      expectedVersionNo?: number
    }): Promise<DocGenSaveResult>
    /** 版本比较：toVersionNo 缺省为当前稿（REQ-GEN-010） */
    diffVersions(req: {
      draftId: string
      fromVersionNo: number
      toVersionNo?: number | null
    }): Promise<TextDiffResult>
    /**
     * AI 修订落库（交付物迭代改写）：修订正文由 agent 修订任务生成，
     * 落 kind='ai_revise' 新版本；修订使定稿回到草稿态。
     * baseVersionNo 为修订基准版本，与当前稿不一致时拒绝（防旧稿覆盖新稿）
     */
    revise(req: {
      draftId: string
      bodyMd: string
      instruction?: string
      baseVersionNo?: number
    }): Promise<DocGenReviseResult>
    /** 导出：Main 弹目录选择对话框并持有 destDir（路径边界同 artifact.export）；null = 用户取消 */
    export(req: { draftId: string; format: 'md' | 'txt' | 'docx' | 'pdf' | 'xlsx' | 'pptx' }): Promise<DocGenExportResult | null>
  }
  /** 派生文档编辑会话与崩溃恢复（DEV-MOD-009 / PRD §18.3 / REQ-GEN-009） */
  editor: {
    /** 进入编辑视图：开启会话；复用崩溃残留会话时 pending=true，须先恢复/放弃 */
    open(draftId: string): Promise<EditorSession>
    /** 正常离开编辑视图（未正常退出时不会调用，下次启动提示恢复） */
    close(sessionId: string): Promise<{ closed: boolean; sessionId: string; keptAutosave: boolean }>
    /** 待恢复项（存在未提交自动保存的未正常退出编辑） */
    pending(caseId?: string): Promise<EditorRecoveryItem[]>
    /** 处理恢复项：recover 保留最近自动保存；discard 回到上次手动版本 */
    resolve(req: {
      sessionId: string
      action: 'recover' | 'discard'
    }): Promise<EditorRecoveryResult>
  }
  /** 待核实队列（PRD 11.4 / R-008：确认、驳回、失效重验） */
  claim: {
    list(req: { caseId?: string; status?: string }): Promise<ClaimSummary[]>
    verify(req: {
      claimId: string
      decision: 'confirmed' | 'rejected'
      note?: string
    }): Promise<{ claimId: string; decision: string; decidedAt: string }>
    recheck(claimId: string): Promise<{
      claimId: string
      status: string
      recovered: boolean
      reasonCode?: string | null
    }>
    /** 矛盾裁决（R-004）：采纳一侧（对侧转待核实）或整体驳回 */
    resolveConflict(req: {
      claimId: string
      decision: 'confirmed' | 'rejected'
      note?: string
    }): Promise<{ claimId: string; decision: string; counterpartId: string; decidedAt: string }>
  }
  /** 产物（M2 收尾：概览 / 内容 / 导出；版本组 = 交付物迭代改写） */
  artifact: {
    /** 每个版本组返回组内最新版本（当前稿），附 groupId/versionNo/versionCount */
    list(caseId: string): Promise<ArtifactMeta[]>
    get(artifactId: string): Promise<ArtifactFull>
    /** 文件类产物（kind=file）原件内容会话：字节分块复用 document.readContent 读取 */
    openContent(req: { artifactId: string }): Promise<DocumentContentInfo>
    /** 直接编辑 AI 生成文档：docx 返回 Word 模型，xlsx/pptx 返回单元格/文本框模型，文本返回源码（无副本） */
    openEditable(req: { artifactId: string }): Promise<ArtifactEditable>
    /** 直接保存回原文档文件；createVersion=true 时在版本组内追加一个编辑版本 */
    saveEditable(req: {
      artifactId: string
      model?: WordNode
      text?: string
      createVersion?: boolean
    }): Promise<ArtifactEditSaveResult>
    /** Excel 单元格增量写回（只改指定单元格） */
    saveSheetEdits(req: {
      artifactId: string
      changes: SheetCellChange[]
      createVersion?: boolean
      confirmLoss?: boolean
    }): Promise<ArtifactEditSaveResult>
    /** PPTX 文本框轻量编辑（形状/版式/图表保持原样） */
    saveSlideTexts(req: {
      artifactId: string
      edits: SlideTextChange[]
      createVersion?: boolean
    }): Promise<ArtifactEditSaveResult>
    /** 在版本组内追加新版本（组、案件与 kind 沿用原产物；新版本即当前稿） */
    saveVersion(req: { artifactId: string; body: Record<string, unknown>; title?: string }): Promise<ArtifactFull>
    /** 版本组历史（传组内任意版本 id 均按组返回，版本号倒序） */
    listVersions(artifactId: string): Promise<ArtifactVersionItem[]>
    /** 回退到组内历史版本：复制该版本追加为新当前版本（历史不删） */
    restoreVersion(req: { artifactId: string; versionNo: number }): Promise<ArtifactFull>
    /** 导出（Main 弹保存对话框，路径不进 Renderer；null = 用户取消） */
    export(req: {
      artifactId: string
      format: ArtifactExportFormat
      /** 用于保存对话框默认文件名 */
      title: string
    }): Promise<ArtifactExportResult | null>
    /**
     * 可视化产物图形导出：Renderer 序列化当前真实图形（SVG 文本或 PNG 字节），
     * Main 弹保存对话框后落盘；仅允许 svg/png，避免把任意二进制写盘。
     */
    saveGraphic(req: {
      artifactId: string
      title: string
      format: 'svg' | 'png'
      /** format=svg 时的 SVG 文本（已做外部资源剥离） */
      svg?: string
      /** format=png 时的 PNG base64（Renderer canvas 栅格化结果） */
      dataBase64?: string
    }): Promise<ArtifactGraphicExportResult | null>
    /** 移除产物（调试残留等非交付物）：概览/气泡卡不再显示，数据与磁盘文件保留 */
    dismiss(artifactId: string): Promise<ArtifactFull>
    /** 产物批注（AI 生成产物；整篇单锚，绑定具体产物版本） */
    annotations(artifactId: string): Promise<ArtifactAnnotation[]>
    addAnnotation(req: { artifactId: string; body: string; quote?: string; color?: string }): Promise<ArtifactAnnotation>
    updateAnnotation(req: { annotationId: string; body: string; color?: string }): Promise<ArtifactAnnotation>
    resolveAnnotation(req: { annotationId: string; resolved: boolean }): Promise<ArtifactAnnotation>
    deleteAnnotation(req: { annotationId: string }): Promise<{ deleted: boolean; annotationId: string }>
  }
  /** 脱敏（M3 / REQ-PRIV；方法与 Core redaction.* RPC 一一对应） */
  redaction: {
    /** 当前档位与生效类型集合 */
    getPolicy(req: { caseId: string }): Promise<RedactionPolicyInfo>
    /** 设置档位（custom 档须给出 customTypes；返回更新后案件） */
    setPolicy(req: {
      caseId: string
      policy: RedactionPolicy
      customTypes?: string[]
    }): Promise<CaseInfo>
    /** 纯检测（不建映射不落库），供 UI 快速扫描 */
    detect(req: { caseId: string; text: string; types?: string[] }): Promise<RedactionHit[]>
    /** 外发差异预览（本机受信 UI：原文 → 别名逐项对照） */
    preview(req: { caseId: string; text: string }): Promise<RedactionPreview>
    /** 按档位脱敏（blockedBy 非空 = 外发必须阻断） */
    apply(req: { caseId: string; text: string }): Promise<RedactionApplyResult>
    /** 本地恢复别名（仅本机受信场景：产物展示/导出） */
    restore(req: { caseId: string; redactedText: string }): Promise<{ restoredText: string }>
    /** 实体映射清单（含解密原文；status 过滤 needs_human 可得待确认队列） */
    listEntities(req: { caseId: string; status?: RedactionEntityStatus }): Promise<RedactionEntity[]>
    /** 人工确认（保留别名）/ 驳回（删除映射，视为误报） */
    confirmEntities(req: {
      caseId: string
      entityIds: string[]
      action?: 'confirm' | 'reject'
    }): Promise<RedactionConfirmResult>
    /** 案件自定义敏感词清单 */
    listTerms(req: { caseId: string }): Promise<RedactionTerm[]>
    addTerm(req: { caseId: string; term: string }): Promise<{ id: string; caseId: string; term: string }>
    removeTerm(req: { caseId: string; termId: string }): Promise<{ ok: boolean; id: string }>
  }
  /** 技能（REQ-AI-003 / REQ-AI-007；安装走 Agent 工具 skill_install，事件流可见） */
  skill: {
    list(): Promise<SkillInfo[]>
    remove(req: { skillId: string }): Promise<{ removed: boolean }>
  }
}
