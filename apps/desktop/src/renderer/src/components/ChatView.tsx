import { useEffect, useRef, useState, type JSX } from 'react'
import type { ApprovalCard, ArtifactMeta } from '@lawbuddy/contracts'
import logoMark from '../assets/lawbuddy-avatar.png'
import { skillDisplay } from '../lib/capabilityMeta'
import { MODES, useApp, type CiteMarkInfo, type Msg, type TaskChatItem } from '../state/store'
import { BodyView } from './BodyView'
import { DocGenMessageView } from './DocGenView'
import { CiteMark } from './CiteMark'
import { HtmlPreviewCard } from './HtmlPreviewCard'
import { HumanQuestionCard } from './HumanQuestionCard'
import { Icon } from './Icon'
import { MarkdownView, type CiteRenderer } from './markdown/MarkdownView'

const TERMINAL = ['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'CANCELLED', 'FAILED']

/** 任务用时格式化：秒内取整秒，超一分钟显示分秒 */
function formatDuration(ms: number): string {
  if (ms < 1000) return '不足 1 秒'
  const sec = Math.round(ms / 1000)
  if (sec < 60) return `${sec} 秒`
  return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`
}

/** 消息时刻展示（气泡底部）：如 9月24日 16:57 */
function formatMsgTime(ts: number): string {
  const d = new Date(ts)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
}

/** 文件大小展示（文件卡副行）：613.2 KB 样式 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** 气泡底部复制按钮：复制全文后短暂反馈「已复制」 */
function ChatCopyButton({ text }: { text: string }): JSX.Element {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className={`chat-copy${copied ? ' ok' : ''}`}
      title="复制文本"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 2000)
        })
      }}
    >
      <Icon id={copied ? 'i-check' : 'i-copy'} />
      {copied ? '已复制' : '复制'}
    </button>
  )
}

/** 文件类型徽章字母（.ftype 配色按扩展名） */
const FILE_EXT_LETTER: Record<string, string> = {
  docx: 'W', pdf: 'P', xlsx: 'X', pptx: 'P', md: 'M', txt: 'T', csv: 'C', html: 'H'
}

/** 任务气泡底部文件卡：本次任务落盘生成的文档（kind=file 产物），点击在阅读区打开 */
function TaskFileCards({ files }: { files: ArtifactMeta[] }): JSX.Element {
  const openDoc = useApp((s) => s.openDoc)
  return (
    <div className="task-files">
      {files.map((f) => {
        const ext = f.title.includes('.') ? f.title.slice(f.title.lastIndexOf('.') + 1).toLowerCase() : 'file'
        const open = () => openDoc(f.id)
        return (
          <button
            key={f.id}
            className="task-file"
            title={`打开 ${f.title}`}
            onClick={open}
          >
            <span className={`ftype ${ext}${FILE_EXT_LETTER[ext] ? '' : ' file'}`}>
              {FILE_EXT_LETTER[ext] ?? (ext.slice(0, 1).toUpperCase() || 'F')}
            </span>
            <span className="task-file-info">
              <span className="task-file-name">{f.title}</span>
              <span className="task-file-meta">
                {typeof f.sizeBytes === 'number' && (
                  <span className="task-file-size">{formatSize(f.sizeBytes)}</span>
                )}
                {/* 版本锚点（升级方案 §7.4）：文件卡标注该文件对应的草稿版本与草稿态 */}
                {typeof f.docgenVersionNo === 'number' && f.docgenVersionNo > 0 && (
                  <span className="task-file-ver" title="该文件对应的草稿版本号">v{f.docgenVersionNo}</span>
                )}
                {f.draftMarked && (
                  <span className="task-file-draft" title="草稿态：未定稿或仍有待补充占位符">草稿</span>
                )}
              </span>
            </span>
            <Icon id="i-arrow" className="task-file-open" />
          </button>
        )
      })}
    </div>
  )
}

/** 对话视图：消息列表 + 任务时间线 + 引证芯片 + 审计提示（PRD 9.4） */
export function ChatView(): JSX.Element {
  const { chatting, messages, mode, cases, archivedCases, currentCaseId, restart, openCitation } = useApp()
  const docgenDraftId = useApp((s) => s.docgenDraftId)
  const listRef = useRef<HTMLDivElement>(null)
  // 用户停留在底部附近时才自动跟随滚动；向上翻阅历史时不被流式更新强行拉回，
  // 重新滚回底部即恢复跟随；自己发送新消息时始终滚到底部
  const stickBottom = useRef(true)
  const current = cases.find((c) => c.id === currentCaseId)
  // 文书生成关联案件（只读共享材料与案件记忆）：标题用于头部提示
  const linked = current?.linkedCaseId
    ? [...cases, ...archivedCases].find((c) => c.id === current.linkedCaseId)
    : null

  useEffect(() => {
    const el = listRef.current
    if (!el) return
    const last = messages[messages.length - 1]
    if (stickBottom.current || last?.role === 'user') el.scrollTop = el.scrollHeight
  }, [messages])

  const onListScroll = (): void => {
    const el = listRef.current
    if (!el) return
    stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  return (
    // 对话视图：进行中（chatting）或已有历史消息（如打开历史会话）时显示
    <section className={`chat-view${chatting || messages.length > 0 ? ' on' : ''}`} aria-live="polite">
      <div className="chat-head">
        <div className="chat-mode">
          当前模式：<b>{MODES[mode].name}</b>
          {mode === 'ws' && current?.linkedCaseId && (
            <span className="chat-role" title="只读共享该案件的材料与案件记忆">
              · 关联案件：{linked?.title ?? '已删除'}
            </span>
          )}
          {mode === 'ws' && (
            // 对话与预览并存（升级方案 §10 阶段二）：明确告知输入坞此刻是「改当前稿」
            <span
              className="chat-role"
              title={docgenDraftId ? '输入内容将作为修改意见修订当前稿' : '选择模板后可直接发送发起起草'}
            >
              · {docgenDraftId ? '正在修改当前文书稿' : '可起草新文书'}
            </span>
          )}
          {mode === 'yj' && current?.clientRole && (
            <span className="chat-role" title="委托人立场（PRD 13.2：界面持续显示当前立场）">
              · 立场：{current.clientRole}
            </span>
          )}
        </div>
        <button className="btn-restart" onClick={restart}>
          <Icon id="i-new" className="icon" />
          新对话
        </button>
      </div>
      <div className="msg-list" ref={listRef} onScroll={onListScroll}>
        {messages.map((m, i) => (
          <Message key={m.id} msg={m} next={messages[i + 1]} onCite={openCitation} />
        ))}
      </div>
    </section>
  )
}

function Message({
  msg,
  next,
  onCite
}: {
  msg: Msg
  next?: Msg
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const openClaimsQueue = useApp((s) => s.openClaimsQueue)
  const restartTask = useApp((s) => s.restartTask)
  if (msg.role === 'user') {
    return (
      <div className="msg msg-user">
        <div className="msg-user-col">
          {/* 本轮显式选定的技能（/ 菜单选中）：气泡上留痕（展示中文映射名），便于回看该轮绑定了哪些能力 */}
          {(msg.skillNames?.length ?? 0) > 0 && (
            <div className="msg-cap-chips" aria-label="本轮选定的技能">
              {msg.skillNames?.map((name) => {
                const d = skillDisplay({ id: name, name, description: '' })
                return (
                  <span
                    key={`skill:${name}`}
                    className="msg-cap-chip"
                    title={d.desc ? `${d.title}：${d.desc}` : `本轮任务绑定技能：${name}`}
                  >
                    技能 · {d.title}
                  </span>
                )
              })}
            </div>
          )}
          {/* 本轮随问附件（截图/图片直发模型，不导入案件文档）：气泡上留痕便于回看 */}
          {(msg.attachmentNames?.length ?? 0) > 0 && (
            <div className="msg-cap-chips" aria-label="本轮随问附件">
              {msg.attachmentNames?.map((name) => (
                <span key={`att:${name}`} className="msg-cap-chip" title={`随问附件：${name}（仅随本轮提问直发模型，未导入案件文档）`}>
                  附件 · {name}
                </span>
              ))}
            </div>
          )}
          <div className="bubble">{msg.text}</div>
          {/* 底部元信息行：复制原文 + 发送时刻 */}
          <div className="msg-meta">
            {msg.text && <ChatCopyButton text={msg.text} />}
            {msg.createdAt != null && <span className="msg-time">{formatMsgTime(msg.createdAt)}</span>}
          </div>
          {/* 任务到达终态（完成/有警告/停止/失败）后出现：纯图标按钮，回填原文重新编辑 */}
          {next && next.role === 'task' && TERMINAL.includes(next.lifecycle) && (
            <button className="chat-restart-btn" onClick={() => restartTask(next.id)} title="重新编辑">
              <Icon id="i-edit" />
            </button>
          )}
        </div>
      </div>
    )
  }

  const avatar = (
    <div className="avatar ai">
      <img className="logo-img" src={logoMark} alt="" />
    </div>
  )

  // 浏览器调试模式 fallback（typing）
  if (msg.role === 'ai') {
    return 'typing' in msg && msg.typing ? (
      <div className="msg msg-ai">
        {avatar}
        <div className="bubble typing">
          <i></i>
          <i></i>
          <i></i>
        </div>
      </div>
    ) : (
      <div className="msg msg-ai">
        {avatar}
        <div className="bubble">
          <BodyView body={msg.body} onCite={onCite} onPending={openClaimsQueue} />
        </div>
      </div>
    )
  }

  // 真实 Agent 任务消息
  return <TaskMessage msg={msg} onCite={onCite} onPending={openClaimsQueue} />
}

/** Agent 任务消息：自主 Loop 消息流（6 事件纯驱动）；旧固定 Loop 任务走时间线回放 */
function TaskMessage({
  msg,
  onCite,
  onPending
}: {
  msg: Extract<Msg, { role: 'task' }>
  onCite: (docId: string, pageNo: number) => void
  onPending?: () => void
}): JSX.Element {
  const avatar = (
    <div className="avatar ai">
      <img className="logo-img" src={logoMark} alt="" />
    </div>
  )
  const done = TERMINAL.includes(msg.lifecycle)
  const hasChatFlow = msg.chat !== undefined // 自主 Loop 任务（发起时即带 chat 字段）
  // 终态展示兜底：停止瞬间未收到结果、或旧版本会话快照残留的 running 工具调用，
  // 渲染层统一按「已停止」展示，避免任务结束后蓝点仍闪动
  const chat = done
    ? (msg.chat ?? []).map((c) => (c.kind === 'tool' && c.status === 'running' ? { ...c, status: 'stopped' as const } : c))
    : (msg.chat ?? [])
  const attachPreview = useApp((s) => s.attachPreviewUrl)
  // render_html 的可视化预览提升到正文区（折叠外）：不随「思考与执行」收起而被藏住，工具完成即可直接查看
  const previews = groupCalls(chat).filter((c) => (c.html || c.previewUrl) && c.status !== 'error')
  const hasProcess = chat.some((c) => c.kind === 'tool') // 存在工具调用 → 显示「思考与执行」总折叠
  // 最终回答 = 最后一条其后无工具调用的定稿文本（其余 assistant 文本均为轮内思考）
  let answerIdx = -1
  for (let i = chat.length - 1; i >= 0; i--) {
    if (chat[i].kind === 'assistant') {
      if (chat[i].text && !hasToolsAfter(chat, i)) answerIdx = i
      break
    }
  }
  // 任务完成后：一键复制最终回答（轮内思考不计入）
  const fullAnswer = done && answerIdx >= 0 ? (chat[answerIdx].text ?? '') : ''
  // 完成时刻：终态时记录的 endedAt；旧快照无该字段时用 startedAt + 总用时兜底
  const doneTime = done
    ? (msg.endedAt ??
      (msg.startedAt != null && msg.turn?.durationMs != null ? msg.startedAt + msg.turn.durationMs : undefined))
    : undefined
  // 本次任务落盘生成的文档文件（Core 任务收尾登记的 kind=file 产物）
  const artifacts = useApp((s) => s.artifacts)
  const taskFiles = artifacts.filter((a) => a.kind === 'file' && a.taskId === msg.taskId)
  // 发出消息后的空窗期（模型排队/首 token 延迟，常见数秒）：先显示三点「输入中」小气泡防假卡死，
  // 首个事件（思考/工具/流式文本）到达后自动切回正常流程
  const waiting =
    !done &&
    chat.length === 0 &&
    !msg.streamText &&
    !msg.thinkingChars &&
    !(msg.taskPlan && msg.taskPlan.length > 0) &&
    !msg.error &&
    !msg.approval &&
    !msg.questionSet
  if (waiting) {
    return (
      <div className="msg msg-ai">
        {avatar}
        <div className="bubble typing" role="status" aria-label="正在思考">
          <i></i>
          <i></i>
          <i></i>
        </div>
      </div>
    )
  }
  return (
    <div className="msg msg-ai">
      {avatar}
      <div className="bubble">
        {hasChatFlow ? (
          <>
            {/* 任务状态阶段面板已移除：执行反馈由「思考与执行」折叠条与流式光标承担 */}
            {/* 等待用户决策时由问题卡承担交互，不再重复挂起提示 */}
            {!done && (msg.lifecycle === 'WAITING_HUMAN' || msg.lifecycle === 'PAUSED') && !msg.questionSet && (
              <div className="degrade-note">任务已挂起：等待补充材料或人工确认后可从断点继续</div>
            )}
            <div className="chat-flow">
              {/* 任务清单卡：先见计划、再见执行过程（Trae 式叙事） */}
              {msg.taskPlan && msg.taskPlan.length > 0 && <PlanCard steps={msg.taskPlan} />}
              {hasProcess && (
                <ProcessFold
                  chat={chat}
                  skipIdx={answerIdx}
                  done={done}
                  durationMs={msg.turn?.durationMs}
                  startedAt={msg.startedAt}
                  cites={msg.cites}
                  onCite={onCite}
                />
              )}
              {/* 过程与回答之间的灰色分割线（参考 Trae 版式） */}
              {hasProcess && (answerIdx >= 0 || msg.streamText) && <div className="proc-divider" aria-hidden="true" />}
              {answerIdx >= 0 && <AnswerView item={chat[answerIdx]} cites={msg.cites} onCite={onCite} />}
              {/* 文书生成结构化摘要（提纲/槽位来源/缺失字段）：与阅卷模式「摘要 + 底部文件卡」对齐 */}
              {msg.body?.docgen && <DocGenMessageView body={msg.body.docgen} />}
              {/* 深度思考进度：GLM 系强制思考模型思考期无正文输出，长思考时给出可见进度防「假卡死」 */}
              {!done && !msg.streamText && !!msg.thinkingChars && (
                <div className="chat-thinking">
                  <span className="task-spin" aria-hidden="true" />
                  深度思考中…
                </div>
              )}
              {msg.streamText && (
                <div className="chat-assistant streaming">
                  {msg.streamText}
                  {!done && <i className="stream-caret" aria-hidden="true" />}
                </div>
              )}
              {/* 正文区可视化预览：工具 render_html 的产物（支持全屏/源码），始终展开可直接查看 */}
              {previews.length > 0 && (
                <div className="chat-previews">
                  {previews.map((p) => (
                    <HtmlPreviewCard
                      key={p.key}
                      html={p.html}
                      previewUrl={p.previewUrl}
                      onRegistered={p.html ? (url) => attachPreview(p.key, url) : undefined}
                      title={p.title || 'HTML 预览'}
                    />
                  ))}
                </div>
              )}
            </div>
            {msg.turn && done && (
              <div className="turn-note">
                {msg.turn.reason === 'max_rounds'
                  ? msg.turn.forcedFinal
                    ? `已达最大轮数（${msg.turn.rounds} 轮），已基于当前进展生成结论；可继续追问深入`
                    : `已达最大轮数（${msg.turn.rounds} 轮），以上为当前进展，可重新发起续跑`
                  : msg.turn.reason === 'max_tool_calls'
                    ? msg.turn.forcedFinal
                      ? `已达工具调用上限（共 ${msg.turn.rounds} 轮），已基于当前进展生成结论；可继续追问深入`
                      : `已达工具调用上限（共 ${msg.turn.rounds} 轮），以上为当前进展，可重新发起续跑`
                    : msg.turn.finalize && !msg.turn.finalize.ok
                      ? `⚠ 已带警告完成 · 共 ${msg.turn.rounds} 轮`
                      : `已完成 · 共 ${msg.turn.rounds} 轮`}
                {/* 完成守门未通过项（05 方案 §7.3）：文字 + 图标同时表达，不只依赖颜色 */}
                {msg.turn.finalize && !msg.turn.finalize.ok && msg.turn.finalize.warnings.length > 0 && (
                  <ul className="turn-warnings">
                    {msg.turn.finalize.warnings.map((w) => (
                      <li key={`${w.code}-${w.text}`}>⚠ {w.text}</li>
                    ))}
                  </ul>
                )}
                {/* 用时已在总折叠概览条展示，此处避免重复 */}
                {!hasProcess && typeof msg.turn.durationMs === 'number' && (
                  <span> · 用时 {formatDuration(msg.turn.durationMs)}</span>
                )}
              </div>
            )}
            {msg.lifecycle === 'CANCELLED' && (
              <div className="degrade-note">已停止 · 已接收内容不完整，仅供参考；重新发起可从断点续跑</div>
            )}
            {/* 底部元信息行：复制回答 + 完成时刻 */}
            {(fullAnswer || doneTime != null) && (
              <div className="msg-meta">
                {fullAnswer && <ChatCopyButton text={fullAnswer} />}
                {doneTime != null && <span className="msg-time">完成于 {formatMsgTime(doneTime)}</span>}
              </div>
            )}
            {/* 本次任务生成的文档文件卡（置于气泡最后） */}
            {taskFiles.length > 0 && <TaskFileCards files={taskFiles} />}
          </>
        ) : (
          <LegacyTaskBody msg={msg} onCite={onCite} onPending={onPending} />
        )}
        {msg.approval && <ApprovalCardView msgId={msg.id} approval={msg.approval} />}
        {msg.questionSet && (
          <HumanQuestionCard msgId={msg.id} taskId={msg.taskId} questionSet={msg.questionSet} />
        )}
        {msg.error && (
          <div className="task-error">
            <Icon id="i-alert" />
            {msg.error}
          </div>
        )}
      </div>
    </div>
  )
}

/** 工具名 → 可读动作文案（未收录的工具直接显示原名） */
const TOOL_LABEL: Record<string, string> = {
  documents_list: '查看卷宗清单',
  document_search: '全文检索',
  document_get_pages: '读取原文',
  task_plan: '更新任务清单',
  memory_search: '检索案件记忆',
  memory_write: '写入案件记忆',
  case_explore: '检索调查（子代理）',
  contract_create_review: '创建合同审查',
  contract_split_clauses: '拆分合同条款',
  contract_get_clauses: '读取合同条款',
  docgen_list_templates: '查询文书模板',
  docgen_create_draft: '创建文书草稿',
  request_user_decision: '请求用户决策',
  skill_install: '安装技能',
  skill_list: '查看技能',
  skill_remove: '删除技能',
  read_file: '读取文件',
  write_file: '写入文件',
  edit_file: '编辑文件',
  glob_files: '查找文件',
  grep_search: '搜索文件内容',
  bash: '执行命令',
  render_html: '渲染 HTML 预览',
  render_pptx: '生成 PPT 幻灯片'
}

/** 聊天回答内 https 外链经 Main 白名单转交系统浏览器（setWindowOpenHandler） */
function openExternalLink(href: string): void {
  if (/^https:\/\//i.test(href)) window.open(href, '_blank', 'noopener')
}

/** 模型所写标题 → 校验数据键（`标题|页码`）匹配：全等 → 包含（双向）；无页码引用仅按标题匹配 */
function citeKeyOf(title: string, pageNo: number | null, cites: Record<string, CiteMarkInfo>): string | undefined {
  const exact = pageNo != null ? `${title}|${pageNo}` : undefined
  if (exact && cites[exact]) return exact
  for (const k of Object.keys(cites)) {
    const [t, p] = k.split('|')
    // 页码匹配：无页码引用接受任意页码（含 0=整文档级）；带页码引用同页优先、整文档级（0）兜底
    const pageOk = pageNo == null || Number(p) === pageNo || Number(p) === 0
    if (pageOk && t && (t.includes(title) || title.includes(t))) return k
  }
  return undefined
}

/** 引用角标渲染器：按正文出现顺序编号（同一出处复用同号）；无校验数据降级灰角标 */
function makeCiteRenderer(
  cites: Record<string, CiteMarkInfo>,
  onCite: (docId: string, pageNo: number) => void
): CiteRenderer {
  const seqMap = new Map<string, number>()
  let next = 0
  return (title, pageNo, pageEnd) => {
    const resolved = citeKeyOf(title, pageNo, cites)
    if (!resolved && pageNo == null) return null // 无页码且未匹配到案件文档（法条/书名等）：保持原文，不出角标
    const info: CiteMarkInfo = resolved
      ? cites[resolved]
      : { title, pageNo: pageNo ?? 0, pageEnd: pageEnd ?? undefined }
    const key = resolved ?? `${title}|${pageNo ?? 0}`
    let seq = seqMap.get(key)
    if (seq === undefined) {
      next += 1
      seq = next
      seqMap.set(key, seq)
    }
    return <CiteMark seq={seq} info={info} onCite={onCite} />
  }
}

/** assistant 定稿文本其后（至下一个 assistant 前）是否存在工具调用：有 → 该文本为轮内思考/计划 */
function hasToolsAfter(chat: TaskChatItem[], index: number): boolean {
  for (let j = index + 1; j < chat.length; j++) {
    if (chat[j].kind === 'assistant') return false
    if (chat[j].kind === 'tool') return true
  }
  return false
}

/** 执行组内单次工具调用（tool 与其结果按 callId 配对） */
interface ExecCall {
  key: string
  name?: string
  args?: string
  status: 'running' | 'ok' | 'error' | 'stopped'
  resultText?: string
  /** case_explore：子代理进度文案（已读取 N 个文件 · 检索 M 次；完成后保留为统计） */
  progress?: string
  /** render_html：完整 HTML（气泡内预览卡） */
  html?: string
  title?: string
  /** render_html：已注册预览地址（历史会话恢复用） */
  previewUrl?: string
}

/** 将连续 tool/toolResult 条目按 callId 配对成调用列表 */
function groupCalls(items: TaskChatItem[]): ExecCall[] {
  const calls: ExecCall[] = []
  const byId = new Map<string, ExecCall>()
  items.forEach((it, i) => {
    if (it.kind === 'tool') {
      const call: ExecCall = {
        key: it.callId || `tool-${i}`,
        name: it.name,
        args: it.args,
        status: it.status ?? 'running',
        progress: it.progress,
        html: it.html,
        title: it.title,
        previewUrl: it.previewUrl
      }
      calls.push(call)
      if (it.callId) byId.set(it.callId, call)
      return
    }
    if (it.kind !== 'toolResult') return
    const call = (it.callId && byId.get(it.callId)) || calls[calls.length - 1]
    if (!call) return
    if (it.status === 'error') call.status = 'error'
    else if (call.status === 'running') call.status = 'ok'
    if (it.resultPreview) call.resultText = it.resultPreview
  })
  return calls
}

/** 概览统计文案：按工具聚合计数，如「读取原文 2 次，全文检索 1 次」 */
function execSummary(calls: ExecCall[]): string {
  const counts = new Map<string, number>()
  for (const c of calls) {
    const label = TOOL_LABEL[c.name ?? ''] ?? c.name ?? '工具调用'
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  return [...counts.entries()].map(([label, n]) => `${label} ${n} 次`).join('，')
}

/** 步骤是否已完成（兼容历史事件里的中英文同义词，避免清单一直显示 0/N） */
function planStepDone(status: string): boolean {
  return ['done', 'completed', 'complete', 'finished', '已完成', '完成'].includes(status.trim().toLowerCase())
}

/** 会话级任务清单卡片（plan_update 数据源，Trae 式「N/M 个任务已完成」） */
function PlanCard({ steps }: { steps: Array<{ text: string; status: string }> }): JSX.Element {
  const total = steps.length
  const fin = steps.filter((s) => planStepDone(s.status)).length
  return (
    <div className="plan-card" role="group" aria-label="任务清单">
      <div className="plan-head">
        <span className="plan-title">
          <Icon id="i-list" />
          任务清单
        </span>
        <span className={`plan-count${fin >= total ? ' all-done' : ''}`}>
          {fin >= total ? `全部 ${total} 个任务已完成` : `${fin}/${total} 个任务已完成`}
        </span>
      </div>
      <ol className="plan-list">
        {steps.map((s, i) => (
          <li key={`${i}-${s.text}`} className={`plan-step${planStepDone(s.status) ? ' done' : ''}`}>
            <span className="plan-step-mark" aria-hidden="true">
              {planStepDone(s.status) ? <Icon id="i-check" /> : <i className="plan-step-ring" />}
            </span>
            <span className="plan-step-text">{s.text}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

/** 轮内思考折叠行：默认收起仅显示「思考 ›」，展开为竖线引用块（参考 Trae 交互） */
function ThinkRow({ text }: { text: string }): JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <div className={`think-row${open ? ' open' : ''}`}>
      <button className="think-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="think-label">思考</span>
        <Icon id="i-chevr" className="think-caret" />
      </button>
      <div className="think-panel">
        <div className="think-clip">
          <div className="think-body">{text}</div>
        </div>
      </div>
    </div>
  )
}

/** 执行组：概览折叠条 + 展开调用树；执行中自动展开，结束后自动收起为概览（预览产物已提升到正文区，收起不影响查看） */
function ExecGroup({ calls }: { calls: ExecCall[] }): JSX.Element {
  const anyRunning = calls.some((c) => c.status === 'running')
  const [open, setOpen] = useState(anyRunning)
  const wasRunning = useRef(anyRunning)
  useEffect(() => {
    // running 翻转时同步展开/收起（手动操作后的渲染不触发翻转，不受影响）
    if (anyRunning !== wasRunning.current) {
      wasRunning.current = anyRunning
      setOpen(anyRunning)
    }
  }, [anyRunning])
  return (
    <div className={`exec-group${open ? ' open' : ''}${anyRunning ? ' running' : ''}`}>
      <button className="exec-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="exec-dot" aria-hidden="true" />
        <span className="exec-summary">{execSummary(calls)}</span>
        <Icon id="i-chevr" className="exec-caret" />
      </button>
      <div className="exec-panel">
        <div className="exec-clip">
          <div className="exec-tree">
            {calls.map((c) => (
              <div key={c.key} className={`exec-call ${c.status}`}>
                <div className="exec-call-head">
                  <span className="exec-call-dot" aria-hidden="true" />
                  <span className="exec-call-label">{TOOL_LABEL[c.name ?? ''] ?? c.name ?? '工具调用'}</span>
                  {c.args && c.args !== '{}' && <code className="exec-call-args">{c.args}</code>}
                  {c.progress && (
                    <span className={`exec-call-progress${c.status === 'running' ? ' live' : ''}`}>{c.progress}</span>
                  )}
                </div>
                {c.resultText && (
                  <div className={`exec-call-result${c.status === 'error' ? ' error' : ''}`}>
                    <span className="exec-call-result-text">{c.resultText}</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

/** 最终回答（无后续工具调用的定稿文本）：Markdown 平铺渲染，轮次徽章保留 */
function AnswerView({
  item,
  cites,
  onCite
}: {
  item: TaskChatItem
  cites?: Record<string, CiteMarkInfo>
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element | null {
  if (!item.text) return null
  return (
    <div className="chat-assistant">
      {item.round ? <span className="chat-round">第 {item.round} 轮</span> : null}
      {/* 定稿整轮文本是 Markdown 主要载体：加粗/标题/列表/表格按渲染呈现（流式期间仍是纯文本，见 streamText 分支） */}
      <MarkdownView
        source={item.text}
        className="chat-md"
        onLinkClick={openExternalLink}
        // 始终传入渲染器：校验数据未到/缺失（旧会话）时降级灰角标（悬浮提示未找到文档），
        // 数据到达后随重渲染升级为已校验/待核实角标，避免引用显示为纯文本
        renderCitation={makeCiteRenderer(cites ?? {}, onCite)}
        // 方案 A：最终回答中的 ```html 代码块自动渲染为预览卡
        renderHtmlBlock={(html, key) => <HtmlPreviewCard html={html} key={key} />}
      />
    </div>
  )
}

/** 总折叠：整段「思考与执行」收进一条概览条（Trae「任务耗时」样式）；执行中自动展开、结束后自动收起 */
function ProcessFold({
  chat,
  skipIdx,
  done,
  durationMs,
  startedAt,
  cites,
  onCite
}: {
  chat: TaskChatItem[]
  skipIdx: number
  done: boolean
  durationMs?: number
  startedAt?: number
  cites?: Record<string, CiteMarkInfo>
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const totalCalls = chat.filter((c) => c.kind === 'tool').length
  const [open, setOpen] = useState(!done)
  const wasDone = useRef(done)
  useEffect(() => {
    // 任务结束瞬间自动收起（手动展开/收起不触发 done 翻转，不受影响）
    if (done !== wasDone.current) {
      wasDone.current = done
      setOpen(!done)
    }
  }, [done])
  // 执行时长记录：执行中每秒刷新实时计时，结束后固定为 turn_complete 回报的总用时
  const [, setTick] = useState(0)
  useEffect(() => {
    if (done || startedAt == null) return
    const timer = window.setInterval(() => setTick((v) => v + 1), 1000)
    return () => window.clearInterval(timer)
  }, [done, startedAt])
  let timeText: string | null = null
  if (done) {
    if (durationMs != null) timeText = `任务耗时 ${formatDuration(durationMs)}`
  } else if (startedAt != null) {
    timeText = `已用时 ${formatDuration(Math.max(0, Date.now() - startedAt))}`
  }
  // 收起概览文案：执行中显示已调用次数（时长在右侧独立展示），结束后仅剩调用次数
  const summary = !done
    ? `思考与执行中${totalCalls > 0 ? ` · 已调用 ${totalCalls} 次` : ''}`
    : `共 ${totalCalls} 次工具调用`
  return (
    <div className={`proc-fold${open ? ' open' : ''}${done ? '' : ' running'}`}>
      <div className="proc-head">
        <button className="proc-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          <span className="proc-dot" aria-hidden="true" />
          <span className="proc-summary">{summary}</span>
          <Icon id="i-chevr" className="proc-caret" />
        </button>
        {timeText && <span className="proc-time">{timeText}</span>}
      </div>
      <div className="proc-panel">
        <div className="proc-clip">
          <div className="proc-body">
            <ChatFlow chat={chat} skipIdx={skipIdx} cites={cites} onCite={onCite} />
          </div>
        </div>
      </div>
    </div>
  )
}

/** 过程树（总折叠内容）：轮内思考折叠行 + 执行组概览/调用树；skipIdx 的最终回答由外部平铺渲染 */
function ChatFlow({
  chat,
  skipIdx,
  cites,
  onCite
}: {
  chat: TaskChatItem[]
  skipIdx: number
  cites?: Record<string, CiteMarkInfo>
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const nodes: JSX.Element[] = []
  let group: TaskChatItem[] = []
  const flush = (key: number): void => {
    if (group.length === 0) return
    nodes.push(<ExecGroup key={`exec-${key}`} calls={groupCalls(group)} />)
    group = []
  }
  chat.forEach((it, i) => {
    if (i === skipIdx) {
      flush(i)
      return
    }
    if (it.kind !== 'assistant') {
      group.push(it)
      return
    }
    flush(i)
    if (!it.text) return
    if (hasToolsAfter(chat, i)) {
      nodes.push(<ThinkRow key={`think-${i}`} text={it.text} />)
      return
    }
    // 非最终回答的独立文本（无后续工具调用）：按普通回答平铺，避免被误折叠进思考
    nodes.push(
      <AnswerView key={`answer-${i}`} item={it} cites={cites} onCite={onCite} />
    )
  })
  flush(chat.length)
  return <>{nodes}</>
}

/** 旧固定 Loop 任务回放（历史任务）：时间线 + 流式预览 + 最终结构化渲染 */
function LegacyTaskBody({
  msg,
  onCite,
  onPending
}: {
  msg: Extract<Msg, { role: 'task' }>
  onCite: (docId: string, pageNo: number) => void
  onPending?: () => void
}): JSX.Element {
  const done = TERMINAL.includes(msg.lifecycle)
  return (
    <>
      {msg.body ? (
        <>
          <BodyView body={msg.body} onCite={onCite} taskId={msg.taskId} onPending={onPending} />
          {msg.streamIncomplete && (
            <div className="degrade-note">流式响应曾中断，以上为降级后的本地分析（中断内容不作为最终结论）</div>
          )}
        </>
      ) : msg.streamText ? (
        (() => {
          const streamShow = msg.streamPreview ?? msg.streamText
          return (
            <div className={`stream-preview${done ? ' stopped' : ''}`}>
              <div className="stream-head">
                {!done && <span className="task-spin" aria-hidden="true"></span>}
                {msg.lifecycle === 'CANCELLED'
                  ? `已停止 · 已接收 ${msg.streamText.length} 字（内容不完整，仅供参考）`
                  : msg.streamIncomplete
                    ? '生成中断（内容不完整，仅供参考）'
                    : '正在生成…'}
              </div>
              <div className="stream-text">
                {streamShow}
                {!done && <i className="stream-caret" aria-hidden="true" />}
              </div>
            </div>
          )
        })()
      ) : (
        !msg.error && (
          <div className="typing">
            <i></i>
            <i></i>
            <i></i>
          </div>
        )
      )}
    </>
  )
}

const DECISION_LABEL: Record<string, string> = {
  allow_once: '已允许（本次）',
  allow_task: '已允许（本任务）',
  allow_case: '已允许（本会话）',
  deny: '已拒绝'
}

/** 审批卡（PRD 12.5）：用途/目标/数据摘要/授权期限/拒绝后降级；用户做出选择后收起为一行摘要，可点击展开回看 */
function ApprovalCardView({
  msgId,
  approval
}: {
  msgId: string
  approval: { card: ApprovalCard; decided?: string }
}): JSX.Element {
  const respondApproval = useApp((s) => s.respondApproval)
  const [expanded, setExpanded] = useState(false)
  const { card, decided } = approval
  const isShell = card.capability === 'SHELL_EXEC'
  const title = isShell ? '命令执行确认' : '外部调用授权确认'
  if (decided && !expanded) {
    return (
      <button
        type="button"
        className={`approval-collapsed ${decided === 'deny' ? 'denied' : 'approved'}`}
        aria-label={`${title}：${DECISION_LABEL[decided] ?? decided}，点击展开详情`}
        aria-expanded={false}
        onClick={() => setExpanded(true)}
      >
        <Icon id="i-shield" />
        <span className="ap-col-title">{title}</span>
        <span className="ap-col-decision">{DECISION_LABEL[decided] ?? decided}</span>
        <span className="ap-col-chevron" aria-hidden="true">›</span>
      </button>
    )
  }
  return (
    <div className="approval-card" role="alertdialog" aria-label={title}>
      <div className="ap-head">
        <Icon id="i-shield" />
        {title}
        <span className="ap-ttl">{decided ? DECISION_LABEL[decided] ?? decided : `${card.expiresInMinutes} 分钟内有效`}</span>
      </div>
      <div className="ap-body">
        <div className="ap-row">
          <span className="ap-k">{isShell ? '命令' : '接收方'}</span>
          <span className="ap-v">{card.target}</span>
        </div>
        <div className="ap-row">
          <span className="ap-k">数据范围</span>
          <span className="ap-v">{card.dataSummary}</span>
        </div>
        <div className="ap-row">
          <span className="ap-k">参数校验</span>
          <span className="ap-v mono">hash {card.payloadHash}…</span>
        </div>
        <div className="ap-fallback">{card.fallback}</div>
      </div>
      {decided ? (
        <button type="button" className={`ap-decided ${decided === 'deny' ? 'denied' : 'approved'}`} onClick={() => setExpanded(false)}>
          {DECISION_LABEL[decided] ?? decided}（收起）
        </button>
      ) : (
        <div className="ap-actions">
          <button className="btn-primary ap-btn" onClick={() => void respondApproval(msgId, card.approvalId, 'allow_once')}>
            允许一次
          </button>
          <button className="btn-ghost ap-btn" onClick={() => void respondApproval(msgId, card.approvalId, 'allow_case')}>
            本会话内允许
          </button>
          <button className="btn-ghost ap-btn deny" onClick={() => void respondApproval(msgId, card.approvalId, 'deny')}>
            拒绝
          </button>
        </div>
      )}
    </div>
  )
}
