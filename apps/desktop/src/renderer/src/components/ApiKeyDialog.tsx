import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import type {
  ModelProtocol,
  ModelProviderInfo,
  ProviderModelInfo,
  ReasoningPreset,
  SaveProviderRequest
} from '@lawbuddy/contracts'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 推理档位可选项（与 Core 模型注册表一致；off = 关闭思考，需填写对应 API 参数值） */
const LEVEL_OPTIONS = ['off', 'low', 'medium', 'high', 'max'] as const
const LEVEL_LABEL: Record<string, string> = {
  off: 'off（关闭思考）',
  low: 'low（轻量）',
  medium: 'medium（均衡）',
  high: 'high（深入）',
  max: 'max（最深）'
}

/**
 * 无服务商预设时的兜底档位（自定义服务商 / 千问等）。
 * 有预设时一律用服务商声明的官方档位，不同厂商取值不同
 * （智谱、Kimi 为 low/high/max，没有 medium）。
 */
const FALLBACK_PRESET: ReasoningPreset = {
  levels: ['low', 'medium', 'high'],
  default: 'medium',
  params: { low: 'low', medium: 'medium', high: 'high' }
}

/** 按服务商预设生成默认档位行 */
function presetDrafts(preset: ReasoningPreset): LevelDraft[] {
  const levels = preset.levels.length ? preset.levels : FALLBACK_PRESET.levels
  return levels.map((lv) => ({ level: lv, param: preset.params[lv] || lv }))
}

/** 预设之外还可新增的档位（已选档位不再出现） */
function unusedLevels(preset: ReasoningPreset, used: LevelDraft[]): string[] {
  return preset.levels.filter((lv) => !used.some((x) => x.level === lv))
}

const PROVIDER_ID_RE = /^[a-z][a-z0-9-]{1,39}$/

/** API 协议选项（与 Core model_protocol.SUPPORTED_PROTOCOLS 一致） */
const PROTOCOL_OPTIONS: Array<{ value: ModelProtocol; label: string; path: string }> = [
  { value: 'openai', label: 'OpenAI Chat Completions', path: '/chat/completions' },
  { value: 'openai-responses', label: 'OpenAI Responses API', path: '/responses' },
  { value: 'anthropic', label: 'Anthropic Messages', path: '/messages' }
]

function protocolLabel(value: string): string {
  return PROTOCOL_OPTIONS.find((p) => p.value === value)?.label ?? value
}

/**
 * IPC 错误文本：Electron 自动加的「Error invoking remote method …」前缀对用户
 * 没有意义，统一剥掉；RPC 自身到点超时时说明上游长时间无响应，给可执行的提示。
 */
function rpcErrorText(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  const reason = raw.replace(/^Error invoking remote method '[^']*': (Error: )?/, '').trim()
  if (/^model\.\w+ timeout$/i.test(reason)) {
    return '模型接口长时间无响应，请确认 API 地址可达（含 /v1 路径）、网络通畅或中转站是否在线。'
  }
  return reason || '连接测试失败：未知错误'
}

/** 上下文窗口快捷值 / 最大输出快捷值（点选即填入） */
const CONTEXT_PRESETS = ['128k', '256k', '512k', '1M']
const OUTPUT_PRESETS = ['4k', '16k', '32k', '128k']

interface LevelDraft {
  level: string
  param: string
}

interface ModelDraft {
  id: string
  label: string
  context: string
  maxOutput: string
  vision: boolean
  levels: LevelDraft[]
  /** 默认档位（官方预设可能不是最后一档，编辑时原样保留） */
  defaultLevel: string
}

function toDraft(m: ProviderModelInfo, preset?: ReasoningPreset): ModelDraft {
  // 已保存的档位：内置服务商只保留官方预设内的档位，老数据里的
  // medium 等厂商不接受的值不能继续展示成可下发档位
  const saved = (m.reasoningLevels ?? []).map((lv) => ({
    level: lv,
    param: m.reasoningParams?.[lv] || lv
  }))
  const allowed = preset?.levels ?? []
  const levels = allowed.length ? saved.filter((x) => allowed.includes(x.level)) : saved
  // 0 / 未声明 = 用户未填写，输入框保持留空（占位提示最佳默认值）
  const base = {
    id: m.id,
    label: m.label,
    context: formatTokens(m.contextLimit),
    maxOutput: formatTokens(m.maxOutput),
    vision: !!m.vision
  }
  // 推理默认开启：目录里没有声明档位的模型（老数据或用户没配过）按服务商官方档位展示，
  // 否则打开设置会看到「推理等级」是关的，误以为该模型不支持思考。
  // 用户主动关闭的条目（reasoningEnabled=false）必须保持关闭，不能被这里重置。
  if (levels.length === 0 && m.reasoningEnabled !== false) {
    return { ...blankDraft(preset), ...base }
  }
  return {
    ...base,
    levels,
    defaultLevel:
      allowed.length && !(allowed as string[]).includes(m.reasoningDefault)
        ? (preset?.default || levels[levels.length - 1]?.level || '')
        : m.reasoningDefault || levels[levels.length - 1]?.level || ''
  }
}

function blankDraft(preset?: ReasoningPreset): ModelDraft {
  const p = preset?.levels?.length ? preset : FALLBACK_PRESET
  return {
    id: '',
    label: '',
    // 新增模型默认留空：由网关按最佳默认值处理，用户可随时填写或点快捷值
    context: '',
    maxOutput: '',
    // 图片输入默认勾选（多数主流模型支持）；实际不支持时会在测试连通 / 发图时报错提示
    vision: true,
    // 思考档位按该服务商官方 API 文档的取值预置（智谱 / Kimi 为 low/high/max，
    // 没有 medium；传官方不支持的档位会被直接拒绝）
    levels: presetDrafts(p),
    defaultLevel: p.default || p.levels[p.levels.length - 1]
  }
}

function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n % 1_048_576 === 0) return `${n / 1_048_576}M`
  if (n % 1024 === 0) return `${n / 1024}K`
  return String(n)
}

function parseTokens(raw: string): number {
  const t = raw.trim().toUpperCase()
  const m = /^([\d.]+)\s*([KMG]?)$/.exec(t)
  if (!m) return 0
  const n = Number.parseFloat(m[1])
  if (!Number.isFinite(n)) return 0
  const mult = m[2] === 'K' ? 1024 : m[2] === 'M' ? 1_048_576 : m[2] === 'G' ? 1_073_741_824 : 1
  return Math.round(n * mult)
}

/** 模型行校验：返回首个错误（带行号，便于用户定位） */
function validateModels(drafts: ModelDraft[]): string {
  if (drafts.length === 0) return '请至少添加一个模型。'
  for (let i = 0; i < drafts.length; i += 1) {
    const d = drafts[i]
    if (!d.id.trim()) return `模型 ${i + 1}：模型 ID 不能为空。`
    // 上下文窗口与最大输出允许留空（留空 = 使用最佳默认值）；填了就必须合法
    if (d.context.trim() && parseTokens(d.context) < 1_000) {
      return `模型 ${i + 1}：上下文窗口需为不小于 1000 的数字（支持 K / M 后缀），或留空使用默认值。`
    }
    if (d.maxOutput.trim() && parseTokens(d.maxOutput) < 1_000) {
      return `模型 ${i + 1}：最大输出 token 数需为不小于 1000 的数字（支持 K / M 后缀），或留空使用默认值。`
    }
  }
  const ids = new Set<string>()
  for (let i = 0; i < drafts.length; i += 1) {
    const id = drafts[i].id.trim()
    if (ids.has(id)) return `模型 ${i + 1}：模型 ID「${id}」重复。`
    ids.add(id)
  }
  return ''
}

function toPayload(drafts: ModelDraft[]): SaveProviderRequest['provider']['models'] {
  return drafts.map((d) => {
    const levels = d.levels.map((l) => l.level) as ProviderModelInfo['reasoningLevels']
    // 默认档位只在仍属于已选档位时保留，否则回落到最后一档
    const def = (levels as string[]).includes(d.defaultLevel)
      ? d.defaultLevel
      : (levels[levels.length - 1] ?? '')
    return {
      id: d.id.trim(),
      label: d.label.trim() || d.id.trim(),
      // 留空提交 0：由 Core 网关按最佳默认值处理（不静默改写成某个数值）
      contextLimit: parseTokens(d.context),
      maxOutput: parseTokens(d.maxOutput),
      vision: d.vision,
      // 显式回写开关状态：关掉时 reasoningLevels 为空且 reasoningEnabled=false，
      // 下次打开设置不会被「推理默认开启」重新打开
      reasoningEnabled: d.levels.length > 0,
      reasoningLevels: levels,
      reasoningDefault: def as ProviderModelInfo['reasoningDefault'],
      reasoningParams: Object.fromEntries(d.levels.map((l) => [l.level, l.param.trim() || l.level]))
    }
  })
}

/**
 * 上下文窗口 / 最大输出的一行输入（参考设计：左侧固定标签 + 输入框 + 右侧快捷值）。
 * 输入框允许留空 —— 留空表示交给网关按最佳默认值处理。
 */
function TokenLimitRow({
  label,
  value,
  presets,
  onChange
}: {
  label: string
  value: string
  presets: string[]
  onChange: (next: string) => void
}): JSX.Element {
  return (
    <div className="ak-token-row">
      <div className="ak-token-field">
        <span className="ak-token-label">{label}</span>
        <input
          className="ak-token-input"
          placeholder="请输入数值，留空则使用最佳默认值"
          value={value}
          aria-label={`上下文窗口${label}`}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
      <div className="ak-token-presets">
        {presets.map((p) => (
          <button
            key={p}
            type="button"
            className={`ak-token-preset${value.trim().toUpperCase() === p.toUpperCase() ? ' on' : ''}`}
            onClick={() => onChange(value.trim().toUpperCase() === p.toUpperCase() ? '' : p)}
          >
            {p}
          </button>
        ))}
      </div>
    </div>
  )
}

/**
 * API 协议选择（参考图四：收起态显示当前协议，展开为带勾选的选项列表）。
 * 三种协议分别对应 /chat/completions、/responses、/messages 三个请求路径。
 */
function ProtocolSelect({
  buttonId,
  value,
  onChange
}: {
  /** 与外部 <label htmlFor> 关联的按钮 id */
  buttonId?: string
  value: ModelProtocol
  onChange: (next: ModelProtocol) => void
}): JSX.Element {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent): void => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <div className="ak-select" ref={boxRef}>
      <button
        type="button"
        id={buttonId}
        className="ak-select-btn"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{protocolLabel(value)} 格式</span>
        <Icon id="i-caret" className={`ak-caret${open ? ' open' : ''}`} />
      </button>
      {open && (
        <div className="ak-select-pop" role="listbox" aria-label="API 协议">
          {PROTOCOL_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              role="option"
              aria-selected={opt.value === value}
              className={`ak-select-opt${opt.value === value ? ' on' : ''}`}
              onClick={() => {
                onChange(opt.value)
                setOpen(false)
              }}
            >
              <span>{opt.label} 格式</span>
              {opt.value === value && <Icon id="i-check" className="ak-select-check" />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 模型目录编辑区（内置服务商与自定义服务商共用） */
function ModelDirectoryEditor({
  providerId,
  base,
  apiKey,
  protocol = 'openai',
  preset,
  drafts,
  onChange,
  showReasoning,
  customized = false,
  onReset,
  onError,
  testing,
  testResults,
  onTest
}: {
  providerId: string
  base: string
  apiKey: string
  /** 探测模型列表时使用的协议（决定鉴权头与默认路径） */
  protocol?: ModelProtocol
  /** 该服务商官方声明的推理档位（新建模型 / 重新开启推理时按此生成） */
  preset?: ReasoningPreset
  drafts: ModelDraft[]
  onChange: (next: ModelDraft[]) => void
  /** 是否展示推理等级编辑（自定义服务商可见；内置预设沿用官方档位） */
  showReasoning: boolean
  /** 模型目录是否已被用户覆盖（标题下显示来源提示） */
  customized?: boolean
  /** 恢复官方默认模型目录（内置服务商覆盖过时提供） */
  onReset?: () => void
  onError: (msg: string) => void
  /** 连通测试状态由对话框统一持有：保存后自动触发，结果按模型 ID 分别展示 */
  testing: boolean
  /** 模型 ID → 测试结果；空字符串键 = 没有模型时的服务商级探测結果 */
  testResults: Record<string, { ok: boolean; text: string }>
  /** 测试一批模型（带各自的「图片」勾选状态；空数组表示只探测地址与密钥） */
  onTest: (models: Array<{ id: string; vision: boolean }>) => void
}): JSX.Element {
  const [fetching, setFetching] = useState(false)

  const patch = (index: number, next: Partial<ModelDraft>): void => {
    onChange(drafts.map((d, i) => (i === index ? { ...d, ...next } : d)))
  }

  const fetchModels = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    if (!base.trim()) {
      onError('请先填写 API 地址，再获取可用模型。')
      return
    }
    setFetching(true)
    try {
      const rows = await bridge.model.fetchModels({
        providerId,
        protocol,
        base: base.trim(),
        ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {})
      })
      if (rows.length === 0) {
        onError('该服务商未返回可用模型，请检查 API 地址与密钥。')
        return
      }
      // 探测结果替换当前目录；已有模型的推理档位按 id 保留，避免丢失手工配置
      const prev = new Map(drafts.map((d) => [d.id, d]))
      onChange(
        rows.map((r) => {
          const old = prev.get(r.id)
          return {
            id: r.id,
            label: r.label || r.id,
            context: formatTokens(r.contextLimit),
            maxOutput: formatTokens(r.maxOutput),
            // 拉取到的新模型按默认能力初始化（图片支持 / 思考档位按服务商官方预设），
            // 已有模型保留用户手工配置
            vision: old?.vision ?? true,
            levels: old?.levels ?? blankDraft(preset).levels,
            defaultLevel: old?.defaultLevel ?? blankDraft(preset).defaultLevel
          }
        })
      )
    } catch (e) {
      onError(`获取可用模型失败：${rpcErrorText(e)}`)
    } finally {
      setFetching(false)
    }
  }

  return (
    <div className="ak-models">
      <div className="ak-models-head">
        <div className="ak-models-heading">
          <span className="ak-models-title">模型目录</span>
          {onReset && (
            <span className="ak-models-source">
              {customized ? '已自定义模型目录' : '内置不预设模型，需自行添加'}
            </span>
          )}
        </div>
        <div className="ak-models-actions">
          {onReset && (
            <button className="ak-link" disabled={!customized} onClick={onReset}>
              清空自定义覆盖
            </button>
          )}
          <button
            className="ak-link"
            disabled={testing}
            onClick={() => onTest(drafts.filter((d) => d.id.trim()).map((d) => ({ id: d.id, vision: d.vision })))}
          >
            {testing ? '测试中…' : '测试连通'}
          </button>
          <button className="ak-link" disabled={fetching} onClick={() => void fetchModels()}>
            {fetching ? '获取中…' : '获取可用模型'}
          </button>
        </div>
      </div>
      {/* 没有模型时才有服务商级结果（只验证地址与密钥）；有模型时结果跟在各模型行下 */}
      {drafts.length === 0 && testResults[''] && (
        <p className={`ak-test${testResults[''].ok ? ' ok' : ' fail'}`}>
          <Icon id={testResults[''].ok ? 'i-check' : 'i-x'} />
          {testResults[''].text}
        </p>
      )}
      {drafts.length === 0 && (
        <p className="form-hint">
          尚未添加模型，请点击下方「添加模型」填写模型 ID；也可先点「获取可用模型」自动拉取。
        </p>
      )}
      {drafts.map((d, i) => (
        <div className="ak-model" key={`${d.id}-${i}`}>
          <div className="ak-model-row">
            <input
              className="form-input"
              placeholder="模型 ID"
              value={d.id}
              onChange={(e) => patch(i, { id: e.target.value })}
              aria-label={`模型 ${i + 1} 的模型 ID`}
            />
            <input
              className="form-input"
              placeholder="显示名称"
              value={d.label}
              onChange={(e) => patch(i, { label: e.target.value })}
              aria-label={`模型 ${i + 1} 的显示名称`}
            />
            <button
              className="icon-btn"
              aria-label={`删除模型 ${i + 1}`}
              title="删除该模型"
              onClick={() => onChange(drafts.filter((_, j) => j !== i))}
            >
              <Icon id="i-x" />
            </button>
          </div>
          {/* 该模型自己的连通结果：每个模型单独验证，不共用第一个模型的结果 */}
          {(() => {
            const key = d.id.trim()
            const res = key ? testResults[key] : undefined
            if (!res) return null
            return (
              <p className={`ak-test${res.ok ? ' ok' : ' fail'}`}>
                <Icon id={res.ok ? 'i-check' : 'i-x'} />
                {res.text}
              </p>
            )
          })()}
          <div className="ak-token">
            <span className="ak-token-title">上下文窗口（Token）</span>
            <TokenLimitRow
              label="输入"
              value={d.context}
              presets={CONTEXT_PRESETS}
              onChange={(v) => patch(i, { context: v })}
            />
            <TokenLimitRow
              label="输出"
              value={d.maxOutput}
              presets={OUTPUT_PRESETS}
              onChange={(v) => patch(i, { maxOutput: v })}
            />
          </div>
          <div className="ak-field">
            <span>输入类型</span>
            <div className="ak-checks">
              <label className="ak-check">
                <input type="checkbox" checked disabled />
                文本
              </label>
              <label className="ak-check">
                <input
                  type="checkbox"
                  checked={d.vision}
                  onChange={(e) => patch(i, { vision: e.target.checked })}
                />
                图片
              </label>
            </div>
          </div>
          {showReasoning && (() => {
            // 该服务商官方档位（无预设时用通用三档）：开关、下拉与「添加档位」都按此收敛，
            // 避免把官方不接受的 medium 之类的值写进配置后发请求必然失败
            const p = preset?.levels?.length ? preset : FALLBACK_PRESET
            const remaining = unusedLevels(p, d.levels)
            return (
            <div className="ak-levels">
              <div className="ak-levels-head">
                <span>推理等级</span>
                <label className="ak-switch">
                  <input
                    type="checkbox"
                    checked={d.levels.length > 0}
                    onChange={(e) =>
                      patch(i, {
                        levels: e.target.checked ? presetDrafts(p) : [],
                        defaultLevel: e.target.checked ? p.default : ''
                      })
                    }
                    aria-label={`模型 ${i + 1} 启用推理等级`}
                  />
                  <span className="ak-switch-track" aria-hidden="true" />
                </label>
              </div>
              {d.levels.length > 0 && (
                <>
                  <div className="ak-level-row ak-level-head">
                    <span>档位</span>
                    <span>API 参数值</span>
                    <span />
                  </div>
                  {d.levels.map((lv, li) => (
                    <div className="ak-level-row" key={`${lv.level}-${li}`}>
                      <select
                        className="form-input"
                        value={lv.level}
                        onChange={(e) =>
                          patch(i, {
                            levels: d.levels.map((x, xi) =>
                              xi === li
                                ? { ...x, level: e.target.value, param: p.params[e.target.value as never] || e.target.value }
                                : x
                            )
                          })
                        }
                        aria-label={`模型 ${i + 1} 第 ${li + 1} 个档位`}
                      >
                        {/* 只列该服务商官方支持的档位（当前值不在其中时保留，避免显示空值） */}
                        {LEVEL_OPTIONS.filter(
                          (opt) => p.levels.includes(opt) || opt === lv.level
                        ).map((opt) => (
                          <option key={opt} value={opt}>
                            {LEVEL_LABEL[opt]}
                          </option>
                        ))}
                      </select>
                      <input
                        className="form-input"
                        placeholder={lv.level === 'off' ? '关闭思考' : `官方取值 ${p.params[lv.level as never] || lv.level}`}
                        value={lv.param}
                        onChange={(e) =>
                          patch(i, {
                            levels: d.levels.map((x, xi) =>
                              xi === li ? { ...x, param: e.target.value } : x
                            )
                          })
                        }
                        aria-label={`模型 ${i + 1} 第 ${li + 1} 个档位的 API 参数值`}
                      />
                      <button
                        className="icon-btn"
                        aria-label="删除档位"
                        onClick={() => patch(i, { levels: d.levels.filter((_, xi) => xi !== li) })}
                      >
                        <Icon id="i-x" />
                      </button>
                    </div>
                  ))}
                  <button
                    className="btn-mini"
                    disabled={remaining.length === 0}
                    title={
                      remaining.length === 0
                        ? '该服务商官方文档声明的推理档位均已添加'
                        : `添加 ${LEVEL_LABEL[remaining[0]]}`
                    }
                    onClick={() =>
                      patch(i, {
                        levels: [
                          ...d.levels,
                          { level: remaining[0], param: p.params[remaining[0] as never] || remaining[0] }
                        ]
                      })
                    }
                  >
                    <Icon id="i-plus" /> 添加档位
                  </button>
                </>
              )}
            </div>
            )
          })()}
        </div>
      ))}
      <button className="btn-mini" onClick={() => onChange([...drafts, blankDraft(preset)])}>
        <Icon id="i-plus" /> 添加模型
      </button>
    </div>
  )
}

/**
 * 模型设置对话框（对齐产品参考设计）：
 * - 「第三方模型提供商」：内置 deepseek / 智谱 / 千问 / Kimi 按官方 API 文档预设，
 *   只需填 API Key 即可用；API 地址与模型目录可在「自定义设置」中覆盖；
 * - 「自定义模型 API」：接入任意 OpenAI Chat Completions 兼容端点（中转站/自部署）。
 * 密钥只写入 Core 加密设置，界面不回显已保存的值。
 */
export function ApiKeyDialog(): JSX.Element | null {
  const {
    apiKeyDialogOpen,
    setApiKeyDialogOpen,
    modelCatalog,
    modelProvider,
    saveModelProvider,
    removeModelProvider,
    resetModelProvider,
    setModelProvider,
    setApiKey,
    toast
  } = useApp()
  const [tab, setTab] = useState<'thirdparty' | 'custom'>('thirdparty')
  const cardRef = useDialogA11y(() => setApiKeyDialogOpen(false), apiKeyDialogOpen)

  // —— 第三方模型提供商 ——
  const [providerId, setProviderId] = useState('')
  const [keyInput, setKeyInput] = useState('')
  const [base, setBase] = useState('')
  const [drafts, setDrafts] = useState<ModelDraft[]>([])
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [error, setError] = useState('')

  // —— 自定义模型 API ——
  const [customId, setCustomId] = useState('')
  const [customLabel, setCustomLabel] = useState('')
  const [customBase, setCustomBase] = useState('')
  const [customProtocol, setCustomProtocol] = useState<ModelProtocol>('openai')
  const [customKey, setCustomKey] = useState('')
  const [customDrafts, setCustomDrafts] = useState<ModelDraft[]>([blankDraft()])
  const [customError, setCustomError] = useState('')
  /** 正在编辑的已创建服务商 ID（为空表示新建） */
  const [editingId, setEditingId] = useState('')

  // —— 连通测试（保存后自动执行；结果按「服务商 + 模型 ID」归属，切服务商即清空） ——
  const [testing, setTesting] = useState(false)
  /**
   * 归属键 → 测试结果：
   * - 服务商级（没有模型时只验证地址与密钥）用 `${providerId}`；
   * - 模型级用 `${providerId}::${modelId}`，保证两家服务商的同名模型结果不串。
   */
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; text: string }>>({})

  /** 把结果按「服务商 + 模型」拆开保存（providerId 为空的自定义草稿用草稿 ID） */
  const testKey = (pid: string, modelId: string): string => `${pid}::${modelId}`

  /** 清掉某个服务商的全部旧结果（换配置 / 重新测试前调用） */
  const dropProviderResults = (prev: Record<string, { ok: boolean; text: string }>, pid: string) => {
    const prefix = `${pid}::`
    const next: Record<string, { ok: boolean; text: string }> = {}
    for (const [k, v] of Object.entries(prev)) {
      if (!k.startsWith(prefix)) next[k] = v
    }
    return next
  }

  /**
   * 逐个模型发一次真实请求，验证「地址 + 协议 + 密钥 + 模型」链路。
   * 传空模型数组时只探测地址与密钥（GET /models，不消耗 token）。
   */
  const runTest = async (target: {
    providerId: string
    base: string
    protocol: ModelProtocol
    apiKey: string
    /** 待测模型：ID + 是否勾选了「图片」（勾了就真的发一张图验证） */
    models: Array<{ id: string; vision: boolean }>
  }): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    const pid = target.providerId.trim()
    if (!target.base.trim()) {
      setTestResults((prev) => ({
        ...dropProviderResults(prev, pid),
        [testKey(pid, '')]: { ok: false, text: '请先填写 API 地址，再测试连通。' }
      }))
      return
    }
    setTesting(true)
    // 开始新一轮测试：先清掉该服务商的旧结果，避免旧结论跟着新配置显示
    setTestResults((prev) => dropProviderResults(prev, pid))
    const declared = target.models.filter((m) => m.id.trim())
    const targets: Array<{ id: string; vision: boolean }> = declared.length
      ? declared
      : [{ id: '', vision: false }]
    for (const item of targets) {
      const model = item.id.trim()
      const key = testKey(pid, model)
      try {
        const res = await bridge.model.testConnection({
          providerId: pid,
          protocol: target.protocol,
          base: target.base.trim(),
          ...(target.apiKey.trim() ? { apiKey: target.apiKey.trim() } : {}),
          ...(model ? { model } : {}),
          // 勾了「图片」就必须真发一张图：只测文本会让不支持视觉的模型也显示通过
          ...(model && item.vision ? { checkVision: true } : {})
        })
        setTestResults((prev) => ({
          ...prev,
          [key]: { ok: res.ok, text: `${res.message}（${res.latencyMs} ms）` }
        }))
      } catch (e) {
        setTestResults((prev) => ({
          ...prev,
          [key]: { ok: false, text: rpcErrorText(e) }
        }))
      }
    }
    setTesting(false)
  }

  /** 保存后测试：密钥已写入本地安全存储，逐个模型验证（没有模型时只探测地址与密钥） */
  const testAfterSave = async (
    savedProviderId: string,
    savedBase: string,
    savedProtocol: ModelProtocol,
    savedModels: ModelDraft[]
  ): Promise<void> => {
    await runTest({
      providerId: savedProviderId,
      base: savedBase,
      protocol: savedProtocol,
      apiKey: '',
      models: savedModels.map((d) => ({ id: d.id, vision: d.vision }))
    })
  }

  const providers = useMemo<ModelProviderInfo[]>(() => modelCatalog?.providers ?? [], [modelCatalog])
  const current: ModelProviderInfo | null = providers.find((p) => p.id === providerId) ?? null
  const customProviders = providers.filter((p) => !p.builtin)

  /** 把全局结果表裁成某一个服务商的视图（键 = 模型 ID，空串 = 服务商级探测） */
  const resultsFor = (pid: string): Record<string, { ok: boolean; text: string }> => {
    const prefix = `${pid.trim()}::`
    return Object.fromEntries(
      Object.entries(testResults)
        .filter(([k]) => k.startsWith(prefix))
        .map(([k, v]) => [k.slice(prefix.length), v])
    )
  }

  // 打开时定位到当前服务商（目录就绪后校正；目录重载后仍指向有效服务商）
  useEffect(() => {
    if (!apiKeyDialogOpen) return
    if (providers.length === 0) return
    if (providers.some((p) => p.id === providerId)) return
    setProviderId(
      providers.some((p) => p.id === modelProvider)
        ? modelProvider
        : (modelCatalog?.defaultProvider ?? providers[0].id)
    )
  }, [apiKeyDialogOpen, providers, providerId, modelProvider, modelCatalog])

  // 切换服务商即载入其官方预设（地址与模型目录）
  useEffect(() => {
    if (!apiKeyDialogOpen) return
    const conf = providers.find((p) => p.id === providerId)
    if (!conf) return
    setBase(conf.base)
    setDrafts(conf.models.map((m) => toDraft(m, conf.reasoningPreset)))
    setKeyInput('')
    setError('')
  }, [apiKeyDialogOpen, providerId, providers])

  // 切换服务商 / Tab / 关闭对话框时清空连通结果：上一家的结论不能跟到下一家
  useEffect(() => {
    setTestResults({})
    setTesting(false)
  }, [providerId, tab, apiKeyDialogOpen])

  if (!apiKeyDialogOpen) return null

  const selectProvider = (id: string): void => {
    setProviderId(id)
    setAdvancedOpen(false)
    // 选中即切换当前服务商：让「已配置」状态与实际发请求的服务商一致
    if (id !== modelProvider) void setModelProvider(id)
  }

  const saveBuiltin = async (): Promise<void> => {
    if (!current) return
    setError('')
    // 还没添加模型时允许先单独保存密钥（可先点「获取可用模型」或「测试连通」验证）
    if (drafts.length === 0) {
      if (keyInput.trim() === '') {
        setError('请至少添加一个模型：点击下方「添加模型」填写模型 ID。')
        return
      }
      await setApiKey(keyInput.trim(), current.id)
      toast('API 密钥已保存；接着添加模型 ID 后即可使用')
      // 保存即自动测一次连通，结果留在当前面板（不关闭对话框）
      await testAfterSave(current.id, base.trim() || current.base, current.protocol, [])
      return
    }
    const msg = validateModels(drafts)
    if (msg) {
      setError(msg)
      return
    }
    const nextBase = base.trim() || current.base
    const payload = toPayload(drafts)
    const unchanged =
      nextBase === current.base &&
      JSON.stringify(payload) ===
        JSON.stringify(current.models.map((m) => toPayload([toDraft(m, current.reasoningPreset)])[0]))
    if (unchanged) {
      // 只改了密钥（或什么都没改）：走密钥写入，不写无意义的目录覆盖
      if (keyInput.trim() !== '') {
        await setApiKey(keyInput.trim(), current.id)
        toast('API 密钥已保存')
        await testAfterSave(current.id, nextBase, current.protocol, drafts)
      } else {
        // 未改动也允许直接验证已保存的配置
        await testAfterSave(current.id, nextBase, current.protocol, drafts)
      }
      return
    }
    const ok = await saveModelProvider({
      provider: {
        id: current.id,
        label: current.label,
        base: nextBase,
        protocol: current.protocol,
        path: current.path,
        models: payload
      },
      ...(keyInput.trim() !== '' ? { apiKey: keyInput.trim() } : {})
    })
    if (!ok) {
      setError('保存失败，请查看提示后重试。')
      return
    }
    toast('配置已保存，正在测试连通…')
    // 保存成功后自动跑一次真实连通测试，结果直接显示在模型目录下方
    await testAfterSave(current.id, nextBase, current.protocol, drafts)
  }

  const resetCustomForm = (): void => {
    setEditingId('')
    setCustomId('')
    setCustomLabel('')
    setCustomBase('')
    setCustomProtocol('openai')
    setCustomKey('')
    setCustomDrafts([blankDraft()])
    setCustomError('')
  }

  const submitCustom = async (): Promise<void> => {
    const pid = customId.trim()
    if (!PROVIDER_ID_RE.test(pid)) {
      setCustomError('Provider ID 需以小写字母开头，仅含小写字母、数字与连字符（2-40 位）。')
      return
    }
    // 新建时不允许与已有服务商（内置或已创建）重名；编辑时 ID 不可改
    if (!editingId && providers.some((p) => p.id === pid)) {
      setCustomError(`Provider ID「${pid}」已存在，请换一个。`)
      return
    }
    if (!/^https?:\/\//.test(customBase.trim())) {
      setCustomError('API 地址需以 http:// 或 https:// 开头。')
      return
    }
    const msg = validateModels(customDrafts)
    if (msg) {
      setCustomError(msg)
      return
    }
    const ok = await saveModelProvider({
      provider: {
        id: pid,
        label: customLabel.trim() || pid,
        base: customBase.trim(),
        protocol: customProtocol,
        models: toPayload(customDrafts)
      },
      ...(customKey.trim() !== '' ? { apiKey: customKey.trim() } : {})
    })
    if (!ok) {
      setCustomError(`${editingId ? '保存' : '创建'}失败，请查看提示后重试。`)
      return
    }
    toast(`${editingId ? '已保存' : '已创建'} ${customLabel.trim() || pid}，正在测试连通…`)
    // 保存成功后自动测试连通：结果展示在表单下方，失败时用户可就地改地址 / 协议 / 密钥
    await testAfterSave(pid, customBase.trim(), customProtocol, customDrafts)
    // 保留表单内容并切到「编辑该服务商」状态：用户看到测试结果后可直接修正再保存
    setEditingId(pid)
  }

  const editCustom = (p: ModelProviderInfo): void => {
    setEditingId(p.id)
    setCustomId(p.id)
    setCustomLabel(p.label)
    setCustomBase(p.base)
    setCustomProtocol(p.protocol)
    setCustomKey('')
    setCustomDrafts(
      p.models.length > 0 ? p.models.map((m) => toDraft(m, p.reasoningPreset)) : [blankDraft(p.reasoningPreset)]
    )
    setCustomError('')
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="模型设置"
      onClick={(e) => {
        if (e.target === e.currentTarget) setApiKeyDialogOpen(false)
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card apikey-card">
        <div className="modal-head">
          <h3>模型设置</h3>
          <button className="icon-btn" aria-label="关闭" onClick={() => setApiKeyDialogOpen(false)}>
            <Icon id="i-x" />
          </button>
        </div>
        <div className="modal-body">
          <div className="viz-tabs" role="tablist" aria-label="模型配置方式">
            <button
              role="tab"
              aria-selected={tab === 'thirdparty'}
              className={`viz-tab${tab === 'thirdparty' ? ' on' : ''}`}
              onClick={() => setTab('thirdparty')}
            >
              第三方模型提供商
            </button>
            <button
              role="tab"
              aria-selected={tab === 'custom'}
              className={`viz-tab${tab === 'custom' ? ' on' : ''}`}
              onClick={() => setTab('custom')}
            >
              自定义模型 API
            </button>
          </div>

          {tab === 'thirdparty' && (
            <>
              <p className="ak-hint">
                已按 DeepSeek、智谱、千问、Kimi 官方 API 文档预设地址与协议，不再预设模型；
                填好 API 密钥与模型 ID 后点「测试连通」验证是否可用。
              </p>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-provider">
                  提供商
                </label>
                <select
                  id="ak-provider"
                  className="form-input"
                  value={providerId}
                  onChange={(e) => selectProvider(e.target.value)}
                >
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}（{p.id}）
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-key">
                  API 密钥
                </label>
                <input
                  id="ak-key"
                  className="form-input"
                  type="password"
                  autoComplete="off"
                  placeholder={current?.configured ? '已保存（留空则不修改）' : '输入该提供商的 API 密钥'}
                  value={keyInput}
                  onChange={(e) => setKeyInput(e.target.value)}
                />
              </div>
              <p className={`ak-status${current?.configured ? '' : ' warn'}`}>
                {current?.configured
                  ? '当前状态：已配置 API 密钥。'
                  : '当前状态：未配置 API 密钥，配置后该提供商的模型才可调用。'}
                密钥仅保存在本机工作空间（加密存储），不进入日志、审计与提示词。
              </p>
              {current?.configured && (
                <div className="ak-inline-actions">
                  <button className="ak-link" onClick={() => void setApiKey('', current.id)}>
                    清除已保存的密钥
                  </button>
                </div>
              )}
              {/* 模型目录直接展开：内置预设不再带模型，填模型 ID 是必经步骤；
                  连通性测试在模型目录标题栏，逐个模型验证并各自展示结果 */}
              {current && (
                <ModelDirectoryEditor
                  key={current.id}
                  providerId={current.id}
                  base={base}
                  apiKey={keyInput}
                  protocol={current.protocol}
                  preset={current.reasoningPreset}
                  drafts={drafts}
                  onChange={setDrafts}
                  showReasoning
                  customized={!!current.customized}
                  onReset={() => void resetModelProvider(current.id)}
                  onError={setError}
                  testing={testing}
                  testResults={resultsFor(current.id)}
                  onTest={(models) =>
                    void runTest({
                      providerId: current.id,
                      base: base || current.base,
                      protocol: current.protocol,
                      apiKey: keyInput,
                      models
                    })
                  }
                />
              )}

              <button
                className="ak-collapse"
                aria-expanded={advancedOpen}
                onClick={() => setAdvancedOpen((v) => !v)}
              >
                <Icon id="i-caret" className={`ak-caret${advancedOpen ? ' open' : ''}`} />
                自定义设置
              </button>
              {advancedOpen && current && (
                <div className="ak-advanced">
                  <div className="form-row">
                    <label className="form-label" htmlFor="ak-base">
                      API 地址
                    </label>
                    <input
                      id="ak-base"
                      className="form-input"
                      placeholder={current.base}
                      value={base}
                      onChange={(e) => setBase(e.target.value)}
                    />
                  </div>
                  <p className="form-hint">
                    请求路径 {current.path} · API 格式 {protocolLabel(current.protocol)}
                  </p>
                  {!current.builtin && (
                    <div className="ak-inline-actions">
                      <button className="ak-link" onClick={() => void removeModelProvider(current.id)}>
                        删除该服务商
                      </button>
                    </div>
                  )}
                </div>
              )}
              {error && <p className="ak-error">{error}</p>}
            </>
          )}

          {tab === 'custom' && (
            <>
              <p className="ak-hint">
                连接中转站、自部署服务或其他兼容接口，填写 API 地址、API 格式和模型即可使用。
              </p>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-custom-id">
                  Provider ID
                </label>
                <input
                  id="ak-custom-id"
                  className="form-input"
                  placeholder="acme-gateway"
                  value={customId}
                  disabled={!!editingId}
                  onChange={(e) => setCustomId(e.target.value)}
                />
              </div>
              <p className="form-hint">
                以小写字母开头的标识，在请求中唯一标识该提供商，并用于派生凭据名。
                {editingId ? '（编辑时不可修改）' : ''}
              </p>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-custom-label">
                  显示名称
                </label>
                <input
                  id="ak-custom-label"
                  className="form-input"
                  placeholder="显示名称"
                  value={customLabel}
                  onChange={(e) => setCustomLabel(e.target.value)}
                />
              </div>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-custom-base">
                  API 地址
                </label>
                <input
                  id="ak-custom-base"
                  className="form-input"
                  placeholder="https://gateway.example/v1"
                  value={customBase}
                  onChange={(e) => setCustomBase(e.target.value)}
                />
              </div>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-custom-protocol">
                  <em>*</em> API 格式
                </label>
                <ProtocolSelect
                  buttonId="ak-custom-protocol"
                  value={customProtocol}
                  onChange={setCustomProtocol}
                />
              </div>
              <div className="form-row">
                <label className="form-label" htmlFor="ak-custom-key">API 密钥</label>
                <input
                  id="ak-custom-key"
                  className="form-input"
                  type="password"
                  autoComplete="off"
                  placeholder="输入 API 密钥"
                  value={customKey}
                  onChange={(e) => setCustomKey(e.target.value)}
                />
              </div>
              <ModelDirectoryEditor
                key={`custom:${editingId || customId.trim()}`}
                providerId={customId.trim()}
                base={customBase}
                apiKey={customKey}
                protocol={customProtocol}
                preset={customProviders.find((p) => p.id === customId.trim())?.reasoningPreset}
                drafts={customDrafts}
                onChange={setCustomDrafts}
                showReasoning
                onError={setCustomError}
                testing={testing}
                testResults={resultsFor(customId.trim())}
                onTest={(models) =>
                  void runTest({
                    providerId: customId.trim(),
                    base: customBase,
                    protocol: customProtocol,
                    apiKey: customKey,
                    models
                  })
                }
              />
              {customError && <p className="ak-error">{customError}</p>}
              {customProviders.length > 0 && (
                <div className="ak-list">
                  <div className="ak-list-row ak-list-head">
                    <span className="ak-models-title">已创建的自定义服务商</span>
                    {editingId && (
                      <button className="ak-link" onClick={resetCustomForm}>
                        取消编辑
                      </button>
                    )}
                  </div>
                  {customProviders.map((p) => (
                    <div className="ak-list-row" key={p.id}>
                      <span className="ak-list-name">{p.label}</span>
                      <span className="ak-list-id">{p.id}</span>
                      <span className="ak-list-meta">{p.models.length} 个模型</span>
                      <button
                        className="btn-mini"
                        onClick={() => editCustom(p)}
                        aria-pressed={editingId === p.id}
                      >
                        {editingId === p.id ? '编辑中' : '编辑'}
                      </button>
                      <button
                        className="icon-btn"
                        aria-label={`删除 ${p.label}`}
                        title="删除该服务商"
                        onClick={() => {
                          void removeModelProvider(p.id)
                          if (editingId === p.id) resetCustomForm()
                        }}
                      >
                        <Icon id="i-x" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn-ghost" onClick={() => setApiKeyDialogOpen(false)}>
            取消
          </button>
          {tab === 'thirdparty' ? (
            <button className="btn-primary" onClick={() => void saveBuiltin()}>
              保存
            </button>
          ) : (
            <button className="btn-primary" onClick={() => void submitCustom()}>
              {editingId ? '保存提供商' : '创建提供商'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
