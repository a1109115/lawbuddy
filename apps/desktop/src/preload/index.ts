import { contextBridge, ipcRenderer } from 'electron'
import type {
  AgentEventNotification,
  AgentTaskEvent,
  AgentTaskInfo,
  ArtifactAnnotation,
  ArtifactEditable,
  ArtifactEditSaveResult,
  ArtifactExportResult,
  ArtifactFull,
  ArtifactGraphicExportResult,
  ArtifactMeta,
  ArtifactVersionItem,
  AuditLogEntry,
  AuditVerifyResult,
  BackupResult,
  BackupVerifyResult,
  CaseInfo,
  ChatAttachment,
  ClaimSummary,
  ContractReviewMeta,
  ContractReviewPayload,
  ContractWorkCopy,
  CoreStatus,
  DocGenDraft,
  DocGenDraftVersion,
  DocGenExportResult,
  EditableCopyResult,
  DocGenOutlineItem,
  DocGenReviseResult,
  DocGenSaveResult,
  DocGenSlotValueInput,
  DocGenTemplate,
  DocGenTemplateDiffResult,
  DocGenDocxTemplateImportInput,
  DocGenDocxTemplateImportResult,
  DocGenTemplateField,
  DocGenTemplateImportInput,
  DocGenTemplateVersion,
  DocumentAnnotation,
  DocumentContentChunk,
  DocumentContentInfo,
  DocumentImportProgress,
  DocumentMeta,
  DocumentPage,
  DocumentTextBlock,
  DocumentUnlockResult,
  EditorRecoveryItem,
  EditorRecoveryResult,
  EditorSession,
  EvidenceBundleResult,
  ImportOutcome,
  HumanQuestionSet,
  MemoryEntry,
  ModelCatalog,
  ModelProviderInfo,
  ModelTestResult,
  FetchedModelInfo,
  OcrCorrection,
  OcrCorrectionResult,
  OcrTaskStatus,
  RedactApplyResult,
  RedactPreviewResult,
  RedactionApplyResult,
  RedactionConfirmResult,
  RedactionEntity,
  RedactionEntityStatus,
  RedactionHit,
  RedactionPolicy,
  RedactionPolicyInfo,
  RedactionPreview,
  RedactionTerm,
  SearchHit,
  SkillInfo,
  LawBuddyBridge,
  TextDiffResult,
  WordNode,
  SheetCellChange,
  SlideTextChange,
  WorkspaceInfo
} from '@lawbuddy/contracts'

/**
 * Preload：Renderer 唯一可见的桥（技术方案 §2.2、§15）。
 * 只暴露白名单 API，不暴露 Node / 原始 IPC / 任意文件路径；
 * 文件导入经一次性 token，路径字符串不进入 Renderer。
 */
const bridge: LawBuddyBridge = {
  platform: process.platform,
  versions: {
    electron: process.versions.electron ?? '',
    chrome: process.versions.chrome ?? ''
  },
  window: {
    minimize: () => ipcRenderer.send('win:minimize'),
    toggleMaximize: () => ipcRenderer.send('win:maximize'),
    close: () => ipcRenderer.send('win:close')
  },
  core: {
    status: () => ipcRenderer.invoke('core:status') as Promise<CoreStatus>,
    ping: () => ipcRenderer.invoke('core:ping') as Promise<{ pong: boolean; latencyMs: number }>,
    onStatus: (cb: (s: CoreStatus) => void) => {
      const listener = (_e: Electron.IpcRendererEvent, s: CoreStatus): void => cb(s)
      ipcRenderer.on('core:status', listener)
      return () => ipcRenderer.removeListener('core:status', listener)
    }
  },
  workspace: {
    ensure: () => ipcRenderer.invoke('workspace:ensure') as Promise<WorkspaceInfo>,
    pickDir: () => ipcRenderer.invoke('workspace:pickDir') as Promise<string | null>,
    open: (rootUri) => ipcRenderer.invoke('workspace:open', rootUri) as Promise<WorkspaceInfo>
  },
  /** HTML 预览：注册完整 HTML 文档，返回 lawbuddy-preview://<id> 供沙箱 iframe 加载 */
  preview: {
    register: (html: string) => ipcRenderer.invoke('preview:register', html) as Promise<string>,
    fetch: (id: string) => ipcRenderer.invoke('preview:fetch', id) as Promise<string | null>,
    /** 预览导出 SVG/PNG（渲染进程序列化，Main 弹窗落盘） */
    saveImage: (req) =>
      ipcRenderer.invoke('preview:saveImage', req) as Promise<ArtifactGraphicExportResult | null>
  },
  case: {
    create: (req) => ipcRenderer.invoke('case:create', req) as Promise<CaseInfo>,
    copyMaterials: (req) => ipcRenderer.invoke('case:copyMaterials', req) as ReturnType<LawBuddyBridge['case']['copyMaterials']>,
    get: (caseId) => ipcRenderer.invoke('case:get', caseId) as Promise<CaseInfo>,
    list: (includeArchived = false) =>
      ipcRenderer.invoke('case:list', includeArchived) as Promise<CaseInfo[]>,
    archive: (caseId) => ipcRenderer.invoke('case:archive', caseId) as Promise<CaseInfo>,
    restore: (caseId) => ipcRenderer.invoke('case:restore', caseId) as Promise<CaseInfo>,
    delete: (req) =>
      ipcRenderer.invoke('case:delete', req) as ReturnType<
        LawBuddyBridge['case']['delete']
      >
  },
  document: {
    pick: (mode?: 'file' | 'folder') =>
      ipcRenderer.invoke('document:pick', mode) as Promise<{
        token: string
        names: string[]
        skipped: number
      } | null>,
    readClipboardImage: () =>
      ipcRenderer.invoke('document:readClipboardImage') as Promise<ChatAttachment | null>,
    pickAttachments: () =>
      ipcRenderer.invoke('document:pickAttachments') as Promise<{
        attachments: ChatAttachment[]
        skipped: number
      } | null>,
    importByToken: (req) =>
      ipcRenderer.invoke('document:import', req) as Promise<ImportOutcome[]>,
    /** 订阅批量导入进度（Main 分批推送）；返回退订函数 */
    onImportProgress: (cb: (p: DocumentImportProgress) => void): (() => void) => {
      const listener = (_e: Electron.IpcRendererEvent, p: DocumentImportProgress): void => cb(p)
      ipcRenderer.on('document:importProgress', listener)
      return () => ipcRenderer.removeListener('document:importProgress', listener)
    },
    list: (caseId) => ipcRenderer.invoke('document:list', caseId) as Promise<DocumentMeta[]>,
    getPages: (documentId) => ipcRenderer.invoke('document:getPages', documentId) as Promise<DocumentPage[]>,
    getTextBlocks: (documentId, pageNo) =>
      ipcRenderer.invoke('document:getTextBlocks', { documentId, pageNo }) as Promise<DocumentTextBlock[]>,
    /** 原件内容会话（REQ-DOC-014）：按格式原样预览，只拿不透明 contentId 与字节分块 */
    openContent: (req) =>
      ipcRenderer.invoke('document:openContent', req) as Promise<DocumentContentInfo>,
    createEditableCopy: (req: { documentId: string }) =>
      ipcRenderer.invoke('document:createEditableCopy', req) as Promise<EditableCopyResult>,
    readContent: (req) =>
      ipcRenderer.invoke('document:readContent', req) as Promise<DocumentContentChunk>,
    closeContent: (contentId) =>
      ipcRenderer.invoke('document:closeContent', contentId) as Promise<{
        closed: boolean
        contentId: string
      }>,
    /** 证据卷宗装订（多份材料 → 封面/目录/页码/书签 PDF；null = 用户取消） */
    exportBundle: (req) =>
      ipcRenderer.invoke('document:exportBundle', req) as Promise<EvidenceBundleResult | null>,
    /** OCR 人工更正（O-008 / Q-009：原值与更正值并存，失效引用转待校验） */
    correctOcr: (req) =>
      ipcRenderer.invoke('document:correctOcr', req) as Promise<OcrCorrectionResult>,
    pageCorrections: (req) =>
      ipcRenderer.invoke('document:pageCorrections', req) as Promise<OcrCorrection[]>,
    /** 页批注（DEV-DOC-010）：quote_missing 读取时计算；归档案件拒绝写入 */
    annotations: (req) =>
      ipcRenderer.invoke('document:annotations', req) as Promise<DocumentAnnotation[]>,
    addAnnotation: (req) =>
      ipcRenderer.invoke('document:addAnnotation', req) as Promise<DocumentAnnotation>,
    updateAnnotation: (req) =>
      ipcRenderer.invoke('document:updateAnnotation', req) as Promise<DocumentAnnotation>,
    resolveAnnotation: (req) =>
      ipcRenderer.invoke('document:resolveAnnotation', req) as Promise<DocumentAnnotation>,
    deleteAnnotation: (req) =>
      ipcRenderer.invoke('document:deleteAnnotation', req) as Promise<{
        deleted: boolean
        annotationId: string
      }>,
    /** 删除单份文档（R-007：受影响 Claim 失效；M-003：相关记忆转失效） */
    delete: (documentId) =>
      ipcRenderer.invoke('document:delete', documentId) as ReturnType<LawBuddyBridge['document']['delete']>,
    redactPreview: (req) =>
      ipcRenderer.invoke('document:redactPreview', req) as Promise<RedactPreviewResult>,
    redactApply: (req) =>
      ipcRenderer.invoke('document:redactApply', req) as Promise<RedactApplyResult>,
    search: (req) => ipcRenderer.invoke('document:search', req) as Promise<SearchHit[]>,
    /** 加密文档解锁（REQ-DOC-004）：密码只经专用窗口输入并直传 Core */
    requestUnlock: (req) =>
      ipcRenderer.invoke('document:requestUnlock', req) as Promise<DocumentUnlockResult>,
    ocr: {
      start: (documentId) => ipcRenderer.invoke('ocr:start', documentId) as Promise<OcrTaskStatus>,
      status: (documentId) => ipcRenderer.invoke('ocr:status', documentId) as Promise<OcrTaskStatus>,
      pause: (documentId) => ipcRenderer.invoke('ocr:pause', documentId) as Promise<OcrTaskStatus>,
      resume: (documentId) => ipcRenderer.invoke('ocr:resume', documentId) as Promise<OcrTaskStatus>,
      cancel: (documentId) => ipcRenderer.invoke('ocr:cancel', documentId) as Promise<OcrTaskStatus>
    }
  },
  audit: {
    verify: () => ipcRenderer.invoke('audit:verify') as Promise<AuditVerifyResult>,
    list: (req) =>
      ipcRenderer.invoke('audit:list', req ?? {}) as Promise<AuditLogEntry[]>
  },
  agent: {
    start: (req) => ipcRenderer.invoke('agent:start', req) as Promise<AgentTaskInfo>,
    getTask: (taskId) => ipcRenderer.invoke('agent:getTask', taskId) as Promise<AgentTaskInfo>,
    resume: (taskId) => ipcRenderer.invoke('agent:resume', taskId) as Promise<AgentTaskInfo>,
    getEvents: (req) =>
      ipcRenderer.invoke('agent:getEvents', req) as Promise<AgentTaskEvent[]>,
    cancel: (taskId) => ipcRenderer.invoke('agent:cancel', taskId) as Promise<AgentTaskInfo>,
    answerHumanQuestions: (req) =>
      ipcRenderer.invoke('agent:answerHumanQuestions', req) as Promise<AgentTaskInfo>,
    getPendingQuestions: (req) =>
      ipcRenderer.invoke('agent:getPendingQuestions', req) as Promise<HumanQuestionSet | null>,
    /** 订阅 Core 推送的任务事件（REQ-AI-001）；返回退订函数 */
    onAgentEvent: (cb: (n: AgentEventNotification) => void): (() => void) => {
      const listener = (_e: Electron.IpcRendererEvent, n: AgentEventNotification): void => cb(n)
      ipcRenderer.on('agent:event', listener)
      return () => ipcRenderer.removeListener('agent:event', listener)
    }
  },
  settings: {
    get: (key) => ipcRenderer.invoke('settings:get', key) as Promise<unknown>,
    set: (req) => ipcRenderer.invoke('settings:set', req) as Promise<{ ok: boolean }>
  },
  /** 模型服务商（内置官方预设 + 自定义 API；目录与能力由 Core 下发） */
  model: {
    catalog: () => ipcRenderer.invoke('model:catalog') as Promise<ModelCatalog>,
    saveProvider: (req) =>
      ipcRenderer.invoke('model:saveProvider', req) as Promise<ModelProviderInfo>,
    removeProvider: (req) =>
      ipcRenderer.invoke('model:removeProvider', req) as Promise<{ removed: boolean }>,
    resetProvider: (req) =>
      ipcRenderer.invoke('model:resetProvider', req) as Promise<{ reset: boolean }>,
    setProvider: (req) =>
      ipcRenderer.invoke('model:setProvider', req) as Promise<{ ok: boolean }>,
    fetchModels: (req) =>
      ipcRenderer.invoke('model:fetchModels', req) as Promise<FetchedModelInfo[]>,
    testConnection: (req) =>
      ipcRenderer.invoke('model:testConnection', req) as Promise<ModelTestResult>
  },
  approval: {
    respond: (req) =>
      ipcRenderer.invoke('approval:respond', req) as Promise<{ approvalId: string; status: string }>
  },
  /** 策略确认（REQ-STR-005：用户选定方案写入审计） */
  strategy: {
    decide: (req) =>
      ipcRenderer.invoke('strategy:decide', req) as ReturnType<
        LawBuddyBridge['strategy']['decide']
      >
  },
  /** 待核实队列（PRD 11.4 / R-008） */
  claim: {
    list: (req) => ipcRenderer.invoke('claim:list', req) as Promise<ClaimSummary[]>,
    verify: (req) => ipcRenderer.invoke('claim:verify', req) as ReturnType<LawBuddyBridge['claim']['verify']>,
    recheck: (claimId) => ipcRenderer.invoke('claim:recheck', claimId) as ReturnType<LawBuddyBridge['claim']['recheck']>,
    /** 矛盾裁决（R-004）：采纳一侧或整体驳回 */
    resolveConflict: (req) =>
      ipcRenderer.invoke('claim:resolveConflict', req) as ReturnType<LawBuddyBridge['claim']['resolveConflict']>
  },
  /** 加密备份与恢复校验（DEV-FND-006 / DB-006；目录与口令均不经 Renderer 落盘） */
  backup: {
    create: (req) => ipcRenderer.invoke('backup:create', req) as Promise<BackupResult | null>,
    verify: (req) => ipcRenderer.invoke('backup:verify', req) as Promise<BackupVerifyResult>
  },
  /** 记忆管理（DEV-AGT-007：列表 / 召回 / 更正 / 删除） */
  memory: {
    list: (req) => ipcRenderer.invoke('memory:list', req) as Promise<MemoryEntry[]>,
    recall: (req) => ipcRenderer.invoke('memory:recall', req) as Promise<MemoryEntry[]>,
    correct: (req) => ipcRenderer.invoke('memory:correct', req) as ReturnType<LawBuddyBridge['memory']['correct']>,
    forget: (req) => ipcRenderer.invoke('memory:forget', req) as ReturnType<LawBuddyBridge['memory']['forget']>
  },
  /** 合同审查（M3 / DEV-MOD-005/006） */
  contract: {
    createOverview: (caseId) => ipcRenderer.invoke('contract:createOverview', caseId) as ReturnType<LawBuddyBridge['contract']['createOverview']>,
    getOverview: (overviewId) => ipcRenderer.invoke('contract:getOverview', overviewId) as ReturnType<LawBuddyBridge['contract']['getOverview']>,
    updateOverview: (req) => ipcRenderer.invoke('contract:updateOverview', req) as ReturnType<LawBuddyBridge['contract']['updateOverview']>,
    getChecklist: (req) => ipcRenderer.invoke('contract:getChecklist', req) as ReturnType<LawBuddyBridge['contract']['getChecklist']>,
    generateChecklist: (req) => ipcRenderer.invoke('contract:generateChecklist', req) as ReturnType<LawBuddyBridge['contract']['generateChecklist']>,
    updateChecklist: (req) => ipcRenderer.invoke('contract:updateChecklist', req) as ReturnType<LawBuddyBridge['contract']['updateChecklist']>,
    confirmChecklist: (req) => ipcRenderer.invoke('contract:confirmChecklist', req) as ReturnType<LawBuddyBridge['contract']['confirmChecklist']>,
    startRiskReview: (req) => ipcRenderer.invoke('contract:startRiskReview', req) as ReturnType<LawBuddyBridge['contract']['startRiskReview']>,
    getRiskRun: (overviewId) => ipcRenderer.invoke('contract:getRiskRun', overviewId) as ReturnType<LawBuddyBridge['contract']['getRiskRun']>,
    applyRiskSuggestion: (req) => ipcRenderer.invoke('contract:applyRiskSuggestion', req) as ReturnType<LawBuddyBridge['contract']['applyRiskSuggestion']>,
    listReviews: (caseId: string) =>
      ipcRenderer.invoke('contract:listReviews', caseId) as Promise<ContractReviewMeta[]>,
    getReview: (reviewId: string) =>
      ipcRenderer.invoke('contract:getReview', reviewId) as Promise<ContractReviewPayload>,
    decideRisk: (req: { riskId: string; decision: string; editedText?: string }) =>
      ipcRenderer.invoke('contract:decideRisk', req) as Promise<ContractReviewPayload>,
    undo: (reviewId: string) =>
      ipcRenderer.invoke('contract:undo', reviewId) as Promise<ContractReviewPayload>,
    redo: (reviewId: string) =>
      ipcRenderer.invoke('contract:redo', reviewId) as Promise<ContractReviewPayload>,
    listWorkCopies: (reviewId: string) =>
      ipcRenderer.invoke('contract:listWorkCopies', reviewId) as Promise<ContractWorkCopy[]>,
    diffWorkCopies: (req: { reviewId: string; fromVersionNo: number; toVersionNo?: number | null }) =>
      ipcRenderer.invoke('contract:diffWorkCopies', req) as Promise<TextDiffResult>
  },
  /** 文书生成（DEV-MOD-007/008） */
  word: {
    openEditor: (targetId) => ipcRenderer.invoke('word:openEditor', targetId) as ReturnType<LawBuddyBridge['word']['openEditor']>,
    listWorkCopies: (caseId) => ipcRenderer.invoke('word:listWorkCopies', caseId) as ReturnType<LawBuddyBridge['word']['listWorkCopies']>,
    createWorkCopy: (req) => ipcRenderer.invoke('word:createWorkCopy', req) as ReturnType<LawBuddyBridge['word']['createWorkCopy']>,
    getModel: (targetId) => ipcRenderer.invoke('word:getModel', targetId) as ReturnType<LawBuddyBridge['word']['getModel']>,
    saveModel: (req) => ipcRenderer.invoke('word:saveModel', req) as ReturnType<LawBuddyBridge['word']['saveModel']>,
    listVersions: (targetId) => ipcRenderer.invoke('word:listVersions', targetId) as ReturnType<LawBuddyBridge['word']['listVersions']>,
    restoreVersion: (req) => ipcRenderer.invoke('word:restoreVersion', req) as ReturnType<LawBuddyBridge['word']['restoreVersion']>,
    diffVersions: (req) => ipcRenderer.invoke('word:diffVersions', req) as ReturnType<LawBuddyBridge['word']['diffVersions']>,
    applyPatches: (req) => ipcRenderer.invoke('word:applyPatches', req) as ReturnType<LawBuddyBridge['word']['applyPatches']>,
    resolveRevisions: (req) => ipcRenderer.invoke('word:resolveRevisions', req) as ReturnType<LawBuddyBridge['word']['resolveRevisions']>,
    export: (targetId) => ipcRenderer.invoke('word:export', targetId) as ReturnType<LawBuddyBridge['word']['export']>
  },
  docgen: {
    installBestPractices: (caseId) => ipcRenderer.invoke('docgen:installBestPractices', caseId) as ReturnType<LawBuddyBridge['docgen']['installBestPractices']>,
    listBestPractices: (caseId) => ipcRenderer.invoke('docgen:listBestPractices', caseId) as ReturnType<LawBuddyBridge['docgen']['listBestPractices']>,
    listTemplates: (caseId: string) =>
      ipcRenderer.invoke('docgen:listTemplates', caseId) as Promise<DocGenTemplate[]>,
    importTemplate: (req: DocGenTemplateImportInput) =>
      ipcRenderer.invoke('docgen:importTemplate', req) as Promise<DocGenTemplate>,
    /** 上传 .docx 范本作为自定义模板（升级方案 §6.1） */
    importDocxTemplate: (req: DocGenDocxTemplateImportInput) =>
      ipcRenderer.invoke('docgen:importDocxTemplate', req) as Promise<DocGenDocxTemplateImportResult>,
    /** 选择 .docx 模板文件（Main 弹出文件选择，返回文件名/大小/字节） */
    pickDocxTemplate: () =>
      ipcRenderer.invoke('docgen:pickDocxTemplate') as Promise<
        { name: string; sizeBytes: number; fileBase64: string } | null
      >,
    updateTemplateFields: (req: { templateId: string; fields: DocGenTemplateField[] }) =>
      ipcRenderer.invoke('docgen:updateTemplateFields', req) as Promise<DocGenTemplate>,
    deleteTemplate: (templateId: string) =>
      ipcRenderer.invoke('docgen:deleteTemplate', templateId) as Promise<{ deleted: boolean; id: string }>,
    listTemplateVersions: (templateId: string) =>
      ipcRenderer.invoke('docgen:listTemplateVersions', templateId) as Promise<DocGenTemplateVersion[]>,
    diffTemplateVersions: (req: { templateId: string; fromVersionNo: number; toVersionNo?: number | null }) =>
      ipcRenderer.invoke('docgen:diffTemplateVersions', req) as Promise<DocGenTemplateDiffResult>,
    rollbackTemplate: (req: { templateId: string; versionNo: number }) =>
      ipcRenderer.invoke('docgen:rollbackTemplate', req) as Promise<DocGenTemplate>,
    listDrafts: (caseId: string) =>
      ipcRenderer.invoke('docgen:listDrafts', caseId) as Promise<DocGenDraft[]>,
    getDraft: (draftId: string) =>
      ipcRenderer.invoke('docgen:getDraft', draftId) as Promise<DocGenDraft>,
    fillSlots: (req: { draftId: string; values: DocGenSlotValueInput[] }) =>
      ipcRenderer.invoke('docgen:fillSlots', req) as Promise<DocGenDraft>,
    updateOutline: (req: { draftId: string; items: DocGenOutlineItem[] }) =>
      ipcRenderer.invoke('docgen:updateOutline', req) as Promise<DocGenDraft>,
    confirmOutline: (draftId: string) =>
      ipcRenderer.invoke('docgen:confirmOutline', draftId) as Promise<DocGenDraft>,
    render: (draftId: string) =>
      ipcRenderer.invoke('docgen:render', draftId) as Promise<DocGenDraft>,
    /** 按 DOCX 模板原格式生成（升级方案 §6.2） */
    renderDocx: (req: { draftId: string; bodyMd?: string; allowUnconfirmed?: boolean }) =>
      ipcRenderer.invoke('docgen:renderDocx', req) as ReturnType<LawBuddyBridge['docgen']['renderDocx']>,
    confirm: (draftId, review) =>
      ipcRenderer.invoke('docgen:confirm', draftId, review) as Promise<DocGenDraft>,
    listVersions: (draftId: string) =>
      ipcRenderer.invoke('docgen:listVersions', draftId) as Promise<DocGenDraftVersion[]>,
    restoreVersion: (req: { draftId: string; versionNo: number }) =>
      ipcRenderer.invoke('docgen:restoreVersion', req) as Promise<DocGenDraft>,
    saveBody: (req: {
      draftId: string
      bodyMd: string
      kind: 'edit' | 'autosave'
      sessionId?: string | null
      expectedVersionNo?: number
    }) => ipcRenderer.invoke('docgen:saveBody', req) as Promise<DocGenSaveResult>,
    diffVersions: (req: { draftId: string; fromVersionNo: number; toVersionNo?: number | null }) =>
      ipcRenderer.invoke('docgen:diffVersions', req) as Promise<TextDiffResult>,
    /** AI 修订落库：修订正文由 agent 修订任务生成后经此落 kind='ai_revise' 版本 */
    revise: (req: { draftId: string; bodyMd: string; instruction?: string; baseVersionNo?: number }) =>
      ipcRenderer.invoke('docgen:revise', req) as Promise<DocGenReviseResult>,
    export: (req: { draftId: string; format: 'md' | 'txt' | 'docx' | 'pdf' | 'xlsx' | 'pptx' }) =>
      ipcRenderer.invoke('docgen:export', req) as Promise<DocGenExportResult | null>
  },
  /** 派生文档编辑会话与崩溃恢复（DEV-MOD-009 / REQ-GEN-009） */
  editor: {
    open: (draftId: string) =>
      ipcRenderer.invoke('editor:open', draftId) as Promise<EditorSession>,
    close: (sessionId: string) =>
      ipcRenderer.invoke('editor:close', sessionId) as Promise<{
        closed: boolean
        sessionId: string
        keptAutosave: boolean
      }>,
    pending: (caseId?: string) =>
      ipcRenderer.invoke('editor:pending', caseId) as Promise<EditorRecoveryItem[]>,
    resolve: (req: { sessionId: string; action: 'recover' | 'discard' }) =>
      ipcRenderer.invoke('editor:resolve', req) as Promise<EditorRecoveryResult>
  },
  /** 产物（M2 收尾：概览 / 内容 / 导出） */
  artifact: {
    list: (caseId) => ipcRenderer.invoke('artifact:list', caseId) as Promise<ArtifactMeta[]>,
    get: (artifactId) => ipcRenderer.invoke('artifact:get', artifactId) as Promise<ArtifactFull>,
    /** 文件类产物原件内容会话（按原格式预览，字节分块经 document.readContent 读取） */
    openContent: (req: { artifactId: string }) =>
      ipcRenderer.invoke('artifact:openContent', req) as Promise<DocumentContentInfo>,
    /** 直接编辑 AI 生成文档：docx → Word 模型；md/html/txt → 源码（无副本） */
    openEditable: (req: { artifactId: string }) =>
      ipcRenderer.invoke('artifact:openEditable', req) as Promise<ArtifactEditable>,
    saveEditable: (req: {
      artifactId: string
      model?: WordNode
      text?: string
      createVersion?: boolean
    }) =>
      ipcRenderer.invoke('artifact:saveEditable', req) as Promise<ArtifactEditSaveResult>,
    saveSheetEdits: (req: {
      artifactId: string
      changes: SheetCellChange[]
      createVersion?: boolean
      confirmLoss?: boolean
    }) =>
      ipcRenderer.invoke('artifact:saveSheetEdits', req) as Promise<ArtifactEditSaveResult>,
    saveSlideTexts: (req: {
      artifactId: string
      edits: SlideTextChange[]
      createVersion?: boolean
    }) =>
      ipcRenderer.invoke('artifact:saveSlideTexts', req) as Promise<ArtifactEditSaveResult>,
    /** 版本组（交付物迭代改写）：组内追加新版本 / 版本历史 / 回退 */
    saveVersion: (req: { artifactId: string; body: Record<string, unknown>; title?: string }) =>
      ipcRenderer.invoke('artifact:saveVersion', req) as Promise<ArtifactFull>,
    listVersions: (artifactId) =>
      ipcRenderer.invoke('artifact:listVersions', artifactId) as Promise<ArtifactVersionItem[]>,
    restoreVersion: (req: { artifactId: string; versionNo: number }) =>
      ipcRenderer.invoke('artifact:restoreVersion', req) as Promise<ArtifactFull>,
    export: (req) =>
      ipcRenderer.invoke('artifact:export', req) as Promise<ArtifactExportResult | null>,
    /** 可视化产物图形导出（Renderer 序列化 SVG / 栅格化 PNG，Main 弹窗落盘） */
    saveGraphic: (req) =>
      ipcRenderer.invoke('artifact:saveGraphic', req) as Promise<ArtifactGraphicExportResult | null>,
    /** 移除产物（调试残留等非交付物）：概览/气泡卡不再显示，数据与磁盘文件保留 */
    dismiss: (artifactId) =>
      ipcRenderer.invoke('artifact:dismiss', artifactId) as Promise<ArtifactFull>,
    /** 产物批注（AI 生成产物；整篇单锚，绑定具体产物版本） */
    annotations: (artifactId) =>
      ipcRenderer.invoke('artifact:annotations', artifactId) as Promise<ArtifactAnnotation[]>,
    addAnnotation: (req) =>
      ipcRenderer.invoke('artifact:addAnnotation', req) as Promise<ArtifactAnnotation>,
    updateAnnotation: (req) =>
      ipcRenderer.invoke('artifact:updateAnnotation', req) as Promise<ArtifactAnnotation>,
    resolveAnnotation: (req) =>
      ipcRenderer.invoke('artifact:resolveAnnotation', req) as Promise<ArtifactAnnotation>,
    deleteAnnotation: (req) =>
      ipcRenderer.invoke('artifact:deleteAnnotation', req) as Promise<{
        deleted: boolean
        annotationId: string
      }>
  },
  /** 脱敏（M3 / REQ-PRIV） */
  redaction: {
    getPolicy: (req) =>
      ipcRenderer.invoke('redaction:getPolicy', req) as Promise<RedactionPolicyInfo>,
    setPolicy: (req) =>
      ipcRenderer.invoke('redaction:setPolicy', req) as Promise<CaseInfo>,
    detect: (req) => ipcRenderer.invoke('redaction:detect', req) as Promise<RedactionHit[]>,
    preview: (req) => ipcRenderer.invoke('redaction:preview', req) as Promise<RedactionPreview>,
    apply: (req) => ipcRenderer.invoke('redaction:apply', req) as Promise<RedactionApplyResult>,
    restore: (req) =>
      ipcRenderer.invoke('redaction:restore', req) as Promise<{ restoredText: string }>,
    listEntities: (req) =>
      ipcRenderer.invoke('redaction:listEntities', req) as Promise<RedactionEntity[]>,
    confirmEntities: (req) =>
      ipcRenderer.invoke('redaction:confirmEntities', req) as Promise<RedactionConfirmResult>,
    listTerms: (req) =>
      ipcRenderer.invoke('redaction:listTerms', req) as Promise<RedactionTerm[]>,
    addTerm: (req) =>
      ipcRenderer.invoke('redaction:addTerm', req) as Promise<{
        id: string
        caseId: string
        term: string
      }>,
    removeTerm: (req) =>
      ipcRenderer.invoke('redaction:removeTerm', req) as Promise<{ ok: boolean; id: string }>
  },
  /** 技能（REQ-AI-003 / REQ-AI-007；安装走 Agent 工具 skill_install） */
  skill: {
    list: () => ipcRenderer.invoke('skill:list', {}) as Promise<SkillInfo[]>,
    remove: (req) => ipcRenderer.invoke('skill:remove', req) as Promise<{ removed: boolean }>
  }
}

contextBridge.exposeInMainWorld('lawbuddy', bridge)
