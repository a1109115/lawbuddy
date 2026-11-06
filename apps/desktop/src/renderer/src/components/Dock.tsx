import { useEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent as RKeyboardEvent, type MouseEvent as RMouseEvent } from 'react'
import type { ReasoningEffort } from '@lawbuddy/contracts'
import { useEscapeKey } from '../hooks/useDialogA11y'
import { skillDisplay } from '../lib/capabilityMeta'
import { THINKING_LABEL, findCatalogModel, findRunningTaskMsg, useApp } from '../state/store'
import { Icon } from './Icon'

/** 各思考档位的副标签（展示名用 THINKING_LABEL；档位清单按当前模型能力动态生成） */
const THINKING_TAGS: Record<Exclude<ReasoningEffort, 'off'>, string> = {
  low: '更快',
  medium: '均衡',
  high: '更深推理',
  max: '最深思考'
}

interface PopPos {
  left: number
  /** 向上展开时的底边锚点 */
  bottom?: number
  /** 向下展开时的顶边锚点 */
  top?: number
}

/** / 菜单条目：已安装技能（REQ-AI-003） */
interface SlashItem {
  key: string
  kind: 'skill'
  id: string
  /** 展示名（中文优先，见 capabilityMeta） */
  name: string
  /** 原始名（用于搜索匹配） */
  rawName: string
  /** 行内一句话简介（灰色，可空） */
  desc: string
  /** 悬停说明卡正文 */
  detail: string
  tag: string
}

/** 底部输入坞（设计规范 §5.3）：内容层 + 工具层；inline 变体用于文书起草首页 */
export function Dock({ variant = 'dock' }: { variant?: 'dock' | 'inline' }): JSX.Element {
  const {
    input,
    setInput,
    send,
    recording,
    toggleRecording,
    model,
    setModel,
    thinking,
    setThinking,
    space,
    openWorkspacePicker,
    openPop,
    setOpenPop,
    pendingAttachments,
    addAttachmentsFromPicker,
    addClipboardImageAttachment,
    removeAttachment,
    apiKeyConfigured,
    setApiKeyDialogOpen,
    modelCatalog,
    skills,
    pendingSkills,
    loadCapabilities,
    togglePendingSkill,
    setSkillDialogOpen,
    mode,
    docgenDraftId,
    cases,
    archivedCases,
    currentCaseId,
    documents,
    setMemoryOpen
  } = useApp()
  const taRef = useRef<HTMLTextAreaElement>(null)
  const [popPos, setPopPos] = useState<PopPos>({ left: 0, bottom: 0 })
  // 弹层向上展开：高度上限取锚点上方可用空间，模型较多时在弹层内滚动而不是顶出视口
  const [popMaxH, setPopMaxH] = useState<number | undefined>(undefined)
  // / 菜单：输入以 / 开头时打开，菜单选中项只影响本次发送的任务
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashIndex, setSlashIndex] = useState(0)
  // 是否用方向键移动过高亮：问题态下按 Enter 仍视为「选中高亮项」而不是发送
  const [slashMoved, setSlashMoved] = useState(false)
  // / 菜单悬停说明卡：fixed 定位挂在条目右侧，避免被菜单 overflow 裁剪
  const [slashTip, setSlashTip] = useState<{ title: string; detail: string; x: number; y: number } | null>(null)
  // 思考强度档位按当前选中模型动态适配（Core 模型目录为唯一能力来源）：
  // 支持档位调节的模型按目录声明列出档位；不支持的模型仅说明设置被忽略
  const currentModel = findCatalogModel(modelCatalog, model)
  const thinkingLevels_: ReasoningEffort[] = currentModel?.reasoningLevels ?? []
  const thinkingAdjustable = thinkingLevels_.length > 0
  // 独立的思考强度设置：只列当前模型自己声明的档位（不再统一列全部档位）
  const thinkingLevels: Array<{ id: ReasoningEffort; label: string; tag: string }> = thinkingLevels_.map(
    (lv) => ({
      id: lv,
      label: THINKING_LABEL[lv],
      // off 档用于「关闭思考」的模型；其余档位给一句用途说明
      tag: lv === 'off' ? '关闭' : THINKING_TAGS[lv as Exclude<ReasoningEffort, 'off'>] ?? ''
    })
  )
  // 当前档位可能来自上一个模型的记忆值、而新模型不声明该档位：展示与实际下发
  // 都要收敛到该模型支持的档位（网关对不支持的值同样按未设置处理）
  const activeThinking: ReasoningEffort = thinkingLevels_.includes(thinking)
    ? thinking
    : (thinkingLevels_[0] ?? 'off')
  // 模型选择列表：由目录展开（内置预设 + 自定义服务商模型），默认服务商模型排在最前
  const modelOptions = useMemo(() => {
    const providers = modelCatalog?.providers ?? []
    const ordered = [
      ...providers.filter((p) => p.id === modelCatalog?.defaultProvider),
      ...providers.filter((p) => p.id !== modelCatalog?.defaultProvider)
    ]
    return ordered.flatMap((p) =>
      p.models.map((m) => ({ id: m.id, label: m.label, provider: p.label }))
    )
  }, [modelCatalog])
  // 任务运行中：发送按钮变为停止按钮（同一位置，点击即停止当前任务）
  const runningTask = useApp((s) => findRunningTaskMsg(s.messages))
  const stopTask = useApp((s) => s.stopTask)
  // 「问AI」等外部入口加载选中文本后触发：聚焦输入框
  const inputFocusTick = useApp((s) => s.inputFocusTick)
  useEffect(() => {
    if (inputFocusTick > 0) taRef.current?.focus()
  }, [inputFocusTick])

  // 输入高度自适应（最大 96px，对应原型行为）
  useEffect(() => {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`
  }, [input])

  // / 菜单开关与过滤词：跟随输入内容（仅当整条输入是 / 命令时打开）。
  // / 后到首个空白为止的片段只有在能命中条目时才作为搜索过滤词；
  // 整句任务问题（如「/请梳理本案证据链并指出薄弱环节」）命中不了任何条目，
  // 此时回退为全量列表并把输入视为问题正文，不再出现「没有匹配项」死胡同
  const slashRaw = input.startsWith('/') ? input.slice(1) : ''
  const slashTermEnd = slashRaw.search(/\s/)
  const slashTerm = slashTermEnd === -1 ? slashRaw : slashRaw.slice(0, slashTermEnd)
  const slashQuery = slashTerm.toLowerCase()
  // / 后是否带有任务问题正文（决定 Enter 是发送还是选中，以及选中后保留正文）
  const slashHasQuestion = slashRaw.trim().length > 0
  useEffect(() => {
    setSlashMoved(false)
    if (input.startsWith('/')) {
      if (!slashOpen) {
        const r = taRef.current?.getBoundingClientRect()
        if (r) setPopPos({ left: Math.min(r.left, window.innerWidth - 320), bottom: window.innerHeight - r.top + 8 })
        void loadCapabilities() // 打开时刷新技能清单（安装后立即可见）
        setSlashIndex(0)
      }
      setSlashOpen(true)
    } else {
      setSlashOpen(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input])

  const { items: slashItems, searching: slashSearching } = useMemo<{ items: SlashItem[]; searching: boolean }>(() => {
    const items: SlashItem[] = skills
      .filter((s) => s.enabled)
      .map((s) => {
        const d = skillDisplay(s)
        return {
          key: `skill:${s.id}`,
          kind: 'skill' as const,
          id: s.id,
          name: d.title,
          rawName: s.name,
          desc: d.desc ?? '',
          detail: d.detail,
          tag: '技能'
        }
      })
    if (!slashQuery) return { items, searching: false }
    const filtered = items.filter((it) => `${it.name} ${it.rawName} ${it.desc}`.toLowerCase().includes(slashQuery))
    if (!filtered.length) {
      // 过滤词没有命中任何条目（例如把整句任务问题误当成过滤词）：
      // 回退为全量列表，此时 Enter 直接发送问题而不是选中技能
      return { items, searching: false }
    }
    return { items: filtered, searching: true }
  }, [skills, slashQuery])

  // 列表清空（技能全部移除）时收起悬停说明卡，避免残留孤立提示
  useEffect(() => {
    if (!slashItems.length) setSlashTip(null)
  }, [slashItems])

  // 点击其他区域关闭弹层（模型/工作空间 Popover 与 / 菜单共用）
  useEffect(() => {
    if (!openPop && !slashOpen) return
    const close = (): void => {
      setOpenPop(null)
    }
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [openPop, slashOpen, setOpenPop])

  useEscapeKey(() => {
    setOpenPop(null)
    if (slashOpen) {
      setSlashOpen(false)
      // 关闭 / 菜单：搜索词随触发段一起清掉；任务问题正文保留（只去掉触发用的 /）
      if (input.startsWith('/') && !slashSearching && slashHasQuestion) {
        setInput(input.slice(1).replace(/^\s+/, ''))
      } else {
        setInput('')
      }
    }
  }, openPop !== null || slashOpen)

  const pickSlash = (item: SlashItem): void => {
    togglePendingSkill(item.id)
    setSlashOpen(false)
    // 选中后剥离 / 触发段：搜索过滤词连同 / 一起去掉；任务问题正文保留（只去掉 /）
    if (input.startsWith('/')) {
      setInput(
        slashSearching ? slashRaw.slice(slashTerm.length).replace(/^\s+/, '') : input.slice(1).replace(/^\s+/, '')
      )
    }
    taRef.current?.focus()
  }

  // 从 / 菜单打开管理对话框：收起菜单并剥离触发用的 /，随后打开对应管理弹窗
  const openManager = (open: () => void): void => {
    setSlashOpen(false)
    setSlashTip(null)
    setOpenPop(null)
    if (input.startsWith('/')) setInput(input.slice(1).replace(/^\s+/, ''))
    open()
  }

  // 发送前剥离触发 / 菜单的前缀（如「/根据现有证据…」→「根据现有证据…」），避免 / 进入任务文本
  const sendQuestion = (): void => {
    if (input.startsWith('/')) setInput(input.slice(1).replace(/^\s+/, ''))
    send()
  }

  const togglePop = (name: string) => (e: RMouseEvent<HTMLButtonElement>) => {
    e.stopPropagation()
    if (openPop === name) {
      setOpenPop(null)
      return
    }
    const r = e.currentTarget.getBoundingClientRect()
    const left = Math.min(r.left, window.innerWidth - 230)
    // 上方空间够用（或比下方宽裕）时向上展开，否则向下展开；高度取对应侧可用空间
    const above = r.top - 16
    const below = window.innerHeight - r.bottom - 16
    if (above >= 320 || above >= below) {
      setPopPos({ left, bottom: window.innerHeight - r.top + 8 })
      setPopMaxH(Math.max(160, above))
    } else {
      setPopPos({ left, top: r.bottom + 8 })
      setPopMaxH(Math.max(160, below))
    }
    setOpenPop(name)
  }

  const onSlashKeyDown = (e: RKeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!slashOpen) return false
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      setSlashMoved(true)
      const delta = e.key === 'ArrowDown' ? 1 : -1
      setSlashIndex((i) => (slashItems.length ? (i + delta + slashItems.length) % slashItems.length : 0))
      return true
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      // 搜索态（过滤词命中）或已用方向键移动过高亮：Enter 选中当前高亮项
      if ((slashSearching || slashMoved) && slashItems.length) {
        e.preventDefault()
        pickSlash(slashItems[Math.min(slashIndex, slashItems.length - 1)])
        return true
      }
      // / 后是任务问题正文（非搜索态）：Enter 直接发送问题，不误选技能
      if (slashHasQuestion) {
        e.preventDefault()
        if (!runningTask) sendQuestion() // 任务运行中 Enter 不再发起新任务
        return true
      }
      // 纯 / 触发（无正文）：保持原行为，选中高亮项
      if (slashItems.length) {
        e.preventDefault()
        pickSlash(slashItems[Math.min(slashIndex, slashItems.length - 1)])
        return true
      }
      return false
    }
    return false
  }

  // 锚定芯片上方的弹层统一样式：限制高度并允许内部滚动，避免顶部内容被视口裁掉
  const popStyle = { ...popPos, maxHeight: popMaxH, overflowY: 'auto' as const }
  // 文书起草：只选模板不写文本也可发起（Core 生成内部任务描述，升级方案 §7.2）；
  // 打开草稿后的修订仍要求写修改意见
  const canSend = input.trim().length > 0 || (mode === 'ws' && !docgenDraftId)
  const isDocGen = mode === 'ws'
  const currentCase = [...cases, ...archivedCases].find((c) => c.id === currentCaseId)
  const linked = currentCase?.linkedCaseId
    ? [...cases, ...archivedCases].find((c) => c.id === currentCase.linkedCaseId)
    : null
  const sharedMaterialCount = linked ? documents.filter((d) => d.caseId === linked.id).length : 0
  const placeholder = recording
    ? '正在聆听，请讲话...（演示）'
    : isDocGen
      ? variant === 'inline'
        ? '选模板后直接发送，或补充起草目标与修改要求'
        : docgenDraftId
          ? '输入修改意见，AI 修订当前稿…'
          : '选模板后直接发送，或补充起草目标与修改要求'
      : '有什么法律问题，尽管问我…输入 / 选择技能'
  const pendingChips: Array<{ key: string; label: string; title?: string; onRemove: () => void }> = [
    ...pendingSkills.map((id) => {
      const s = skills.find((x) => x.id === id)
      const d = s ? skillDisplay(s) : null
      return {
        key: `skill:${id}`,
        label: d?.title ?? id,
        title: d?.detail,
        onRemove: () => togglePendingSkill(id)
      }
    })
  ]

  return (
    <div className={variant === 'inline' ? 'dock dock-inline' : 'dock'}>
      {pendingAttachments.length > 0 && (
        <div className="dock-pending" aria-label="本次提问的随问附件（仅随消息直发模型，不导入案件文档）">
          {pendingAttachments.map((a) => (
            <span key={a.id} className="dock-pending-chip" title={`随问附件：${a.name}（${a.mime}）`}>
              <Icon id="i-clip" />
              {a.name}
              <button aria-label={`移除附件 ${a.name}`} onClick={() => removeAttachment(a.id)}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {pendingChips.length > 0 && (
        <div className="dock-pending" aria-label="本次任务启用的技能">
          {pendingChips.map((c) => (
            <span key={c.key} className="dock-pending-chip" title={c.title}>
              {c.label}
              <button aria-label={`移除 ${c.label}`} onClick={c.onRemove}>
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="dock-input-wrap">
        <textarea
          ref={taRef}
          className="dock-input"
          rows={1}
          value={input}
          maxLength={isDocGen ? 5000 : undefined}
          placeholder={placeholder}
          aria-label={isDocGen ? '输入文书起草需求' : '输入法律问题'}
          onChange={(e) => setInput(e.target.value)}
          onPaste={(e) => {
            // 剪贴板里有图片（截图/复制的图片）时作为随问附件直发模型，不走案件导入与 OCR
            const hasImage = Array.from(e.clipboardData.items).some(
              (it) => it.kind === 'file' && it.type.startsWith('image/')
            )
            if (hasImage) {
              e.preventDefault()
              void addClipboardImageAttachment()
            }
          }}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing || e.keyCode === 229) return
            if (onSlashKeyDown(e)) return
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              if (!runningTask) sendQuestion() // 任务运行中 Enter 不再发起新任务
            }
          }}
        />
      </div>
      <div className="dock-bar">
        {!isDocGen && (
          <button
            className={`icon-mic${recording ? ' rec' : ''}`}
            aria-label="语音输入"
            aria-pressed={recording}
            title="语音输入"
            onClick={toggleRecording}
          >
            <Icon id="i-mic" />
          </button>
        )}
        <button className="dock-chip" data-chip="model" onClick={togglePop('model')} title="选择模型">
          <Icon id="i-zap" />
          <span className="chip-label">{model || '未选模型'}</span>
          <Icon id="i-caret" className="icon caret" />
        </button>
        {!isDocGen && (
          <button
            className="dock-chip"
            data-chip="thinking"
            onClick={togglePop('thinking')}
            title={
              thinkingAdjustable
                ? '思考强度（档位按当前模型能力适配，越深越慢）'
                : '当前模型未声明思考档位，可在「模型设置」中为该模型添加'
            }
          >
            <Icon id="i-bulb" />
            <span className="chip-label">
              {thinkingAdjustable ? THINKING_LABEL[activeThinking] : '无档位'}
            </span>
            <Icon id="i-caret" className="icon caret" />
          </button>
        )}
        <button
          className="dock-chip"
          data-chip="upload"
          onClick={() => void addAttachmentsFromPicker()}
          title="添加随问附件（图片/文本，仅本次提问使用，直发模型；正式案卷材料请走案件导入）"
        >
          <Icon id="i-clip" />
          {isDocGen ? '文件' : '上传文件'}
        </button>
        {isDocGen ? (
          <>
            <button className="dock-chip" data-chip="linkcase" onClick={togglePop('linkcase')} title="关联案件（只读共享材料与案件记忆）">
              <Icon id="i-folder" />
              关联案件
              <Icon id="i-caret" className="icon caret" />
            </button>
            <button
              className="dock-chip"
              data-chip="skill"
              onClick={() => setSkillDialogOpen(true)}
              title="选择本次任务使用的技能"
            >
              <Icon id="i-bulb" />
              技能
            </button>
          </>
        ) : (
          <button
            className="dock-chip"
            data-chip="space"
            onClick={togglePop('space')}
            title={space ? `当前工作空间：${space}` : '本地工作空间'}
          >
            <Icon id="i-folder" />
            本地工作空间
            <Icon id="i-caret" className="icon caret" />
          </button>
        )}
        <div className="dock-spacer"></div>
        {variant === 'inline' && isDocGen && <span className="dock-count">{input.length}/5000</span>}
        {runningTask ? (
          <button
            className="btn-send stop"
            aria-label="停止"
            title="停止当前任务"
            onClick={() => void stopTask(runningTask.id, runningTask.taskId)}
          >
            <Icon id="i-square" />
          </button>
        ) : (
          <button className="btn-send" disabled={!canSend} aria-label="发送" onClick={sendQuestion}>
            <Icon id="i-send" />
          </button>
        )}
      </div>

      {openPop === 'model' && (
        <div
          className="pop pop-model"
          role="menu"
          style={{ ...popPos, maxHeight: popMaxH }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="pop-title">选择模型</div>
          {modelOptions.length === 0 && (
            <div className="pop-note">
              <b>尚未添加模型</b>
              <span>内置服务商不再预设模型，请在下方「模型 API Key」中填写模型 ID 并测试连通</span>
            </div>
          )}
          <div className="pop-model-list">
            {modelOptions.map((m) => (
              <button
                key={m.id}
                className={`pop-item${model === m.id ? ' sel' : ''}`}
                onClick={() => setModel(m.id)}
              >
                <Icon id="i-zap" />
                {m.id}
                <span className="tag">{m.provider}</span>
                <Icon id="i-check" className="icon check" />
              </button>
            ))}
          </div>
          <div className="pop-sep" role="separator"></div>
          <button
            className="pop-item pop-key-item"
            onClick={() => {
              setOpenPop(null)
              setApiKeyDialogOpen(true)
            }}
          >
            <Icon id="i-gear" />
            模型 API Key
            <span className={`tag${apiKeyConfigured ? ' local' : ''}`}>{apiKeyConfigured ? '已配置' : '未配置'}</span>
          </button>
        </div>
      )}

      {/* 思考强度：独立弹层，只列当前模型自己声明的档位（参考图二/三的分档胶囊） */}
      {openPop === 'thinking' && (
        <div className="pop" role="menu" style={popStyle} onClick={(e) => e.stopPropagation()}>
          <div className="pop-title">{model ? `${model} · 思考强度` : '思考强度'}</div>
          {thinkingAdjustable ? (
            <>
              <div className="pop-think-row">
                {thinkingLevels.map((t) => (
                  <button
                    key={t.id}
                    className={`pop-think-chip${activeThinking === t.id ? ' on' : ''}`}
                    aria-pressed={activeThinking === t.id}
                    title={t.tag ? `${t.label} · ${t.tag}` : t.label}
                    onClick={() => setThinking(t.id)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <p className="pop-think-hint">
                档位越高思考越深、耗时越长；由该模型自行决定是否思考
              </p>
            </>
          ) : (
            <div className="pop-note">
              <b>当前模型未声明思考档位</b>
              <span>
                {model
                  ? '该模型在模型目录中没有配置档位，因此这里只能关闭。'
                  : '请先在上方「选择模型」中添加并选中一个模型。'}
              </span>
              {model && (
                <button
                  className="ak-link"
                  onClick={() => {
                    setOpenPop(null)
                    setApiKeyDialogOpen(true)
                  }}
                >
                  去「模型设置」为该模型添加推理档位
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {openPop === 'space' && (
        <div className="pop" role="menu" style={popStyle} onClick={(e) => e.stopPropagation()}>
          <div className="pop-title">本地工作空间</div>
          {space ? (
            <div className="pop-note">
              <b title={space} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {space}
              </b>
              <span>当前工作空间 · 案件与文档保存在此目录</span>
            </div>
          ) : (
            <div className="pop-note">
              <b>尚未确定工作空间</b>
              <span>正在读取上次使用的目录</span>
            </div>
          )}
          <button className="pop-item" onClick={() => void openWorkspacePicker()}>
            <Icon id="i-folder" />
            打开其他目录...
          </button>
        </div>
      )}

      {openPop === 'linkcase' && (
        <div className="pop" role="menu" style={popPos} onClick={(e) => e.stopPropagation()}>
          <div className="pop-title">关联案件（只读共享材料与案件记忆）</div>
          {linked ? (
            <>
              <div className="pop-note">
                <b>{linked.title}</b>
                <span>{sharedMaterialCount} 份共享材料 · 只读</span>
              </div>
              <button
                className="pop-item"
                onClick={() => {
                  setOpenPop(null)
                  setMemoryOpen(true)
                }}
              >
                <Icon id="i-list" />
                打开案件记忆
              </button>
            </>
          ) : (
            <div className="slash-hint">当前任务未关联案件。可在任务设置中关联后，共享其材料与案件记忆。</div>
          )}
        </div>
      )}

      {slashOpen && (
        <div className="pop slash-pop" role="listbox" aria-label="技能" style={popPos} onClick={(e) => e.stopPropagation()}>
          <div className="pop-title">技能（选中后仅对本次发送生效）</div>
          {/* 问题态（过滤词没有命中条目，列表回退全量）：提示 Enter 直接发送、点条目附加技能 */}
          {slashHasQuestion && !slashSearching && (
            <div className="slash-hint">没有与「{slashTerm}」匹配的技能；按 Enter 直接发送问题，点击条目则附加到本次任务</div>
          )}
          {slashItems.map((it, i) => {
            const selected = pendingSkills.includes(it.id)
            return (
              <button
                key={it.key}
                role="option"
                aria-selected={selected}
                className={`pop-item slash-item${i === slashIndex ? ' focus' : ''}${selected ? ' sel' : ''}`}
                onMouseEnter={(e) => {
                  setSlashIndex(i)
                  // 说明卡定位：优先条目右侧；空间不足放左侧；纵向贴条目并夹在视口内
                  const r = e.currentTarget.getBoundingClientRect()
                  const width = 264
                  const x = r.right + 10 + width > window.innerWidth ? r.left - width - 10 : r.right + 10
                  setSlashTip({
                    title: `${it.tag} · ${it.name}`,
                    detail: it.detail,
                    x,
                    y: Math.max(8, Math.min(r.top - 6, window.innerHeight - 150))
                  })
                }}
                onMouseLeave={() => setSlashTip(null)}
                onClick={() => pickSlash(it)}
              >
                <Icon id="i-bulb" />
                <span className="slash-name">{it.name}</span>
                {it.desc && <span className="slash-desc">{it.desc}</span>}
                <span className="tag">{it.tag}</span>
                <Icon id="i-check" className="icon check" />
              </button>
            )
          })}
          {skills.length === 0 && (
            <div className="slash-hint">尚无已安装技能；把技能文件或目录发送给助手即可安装</div>
          )}
          <div className="pop-sep" role="separator"></div>
          <button className="pop-item" onClick={() => openManager(() => setSkillDialogOpen(true))}>
            <Icon id="i-bulb" />
            管理技能库…
            <span className="tag">{skills.length}</span>
          </button>
        </div>
      )}

      {/* / 菜单悬停说明卡（深色，参考概览卡样式）；随菜单关闭一起消失 */}
      {slashOpen && slashTip && (
        <div className="slash-tip" role="tooltip" style={{ left: slashTip.x, top: slashTip.y }}>
          <b>{slashTip.title}</b>
          <p>{slashTip.detail}</p>
        </div>
      )}
    </div>
  )
}
