import { useCallback, useEffect, useState, type JSX } from 'react'
import type { DiagnosticsSnapshot, ModelHealth, ProviderConformance } from '@lawbuddy/contracts'
import { errorMessage, getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { Icon } from './Icon'

const CATEGORY_LABEL: Record<string, string> = {
  auth: '认证失败（检查 API Key）',
  billing: '账户余额或计费问题',
  permission: '权限不足',
  rate_limit: '触发限流（可重试）',
  context_overflow: '上下文超长（需压缩或换模型）',
  invalid_request: '请求参数无效',
  format: '响应格式异常',
  timeout: '请求超时（可重试）',
  disconnect: '连接中断（可重试）',
  stale_stream: '流式停滞（已中断，可恢复）',
  provider_unavailable: '服务商不可用（可重试）',
  persistence: '本地持久化失败',
  cancelled: '用户取消',
  unknown: '未分类错误'
}

/** §10.1 回滚开关：UI 必须能真实关闭新能力，而不是只在配置文件里改 */
const RUNTIME_SWITCHES: Array<{
  key: string
  label: string
  hint: string
  /** 关闭后的行为说明（写清楚影响面，避免用户误以为数据被删） */
  offEffect: string
  defaultOn: boolean
}> = [
  {
    key: 'agent.toolSearch',
    label: '中文 Tool Search 渐进披露',
    hint: '按需下发低频工具 Schema，减少上下文占用',
    offEffect: '关闭后所有工具 Schema 全量下发；已登记工具与 Journal 记录保留',
    defaultOn: false
  },
  {
    key: 'agent.contextCompressionV2',
    label: '引证安全上下文压缩',
    hint: '确定性摘要 + 证据索引 + 归档版本',
    offEffect: '关闭后回退旧确定性压缩；已归档对话仍可读取，新压缩不再写归档',
    defaultOn: true
  },
  {
    key: 'agent.learningProposals',
    label: '后台学习提案',
    hint: '记忆/技能只生成待审提案',
    offEffect: '关闭后不再生成新提案；已批准的记忆与技能继续生效',
    defaultOn: false
  },
  {
    key: 'agent.plugins',
    label: '插件子系统总闸',
    hint: 'MCP 与第三方插件（默认禁用）',
    offEffect: '关闭后停用并终止全部插件/MCP 子进程，其工具不再下发',
    defaultOn: false
  },
  {
    key: 'agent.localAutomation',
    label: '受限本地巡检',
    hint: '只读统计待重验引用、失效记忆与待核实项（不自动处理）',
    offEffect: '关闭后拒绝执行任何巡检；不排后台任务，也不会有遗留进程',
    defaultOn: false
  }
]

/**
 * 模型健康（§7.3）：服务商、配置状态、最近错误类别与一致性检查。
 *
 * 只展示 `configured` 布尔与能力描述，**不回传也不显示密钥**；
 * 错误按类别聚合（不含响应正文），用于判断是限流、超时还是认证问题。
 */
export function ModelHealthDialog(): JSX.Element | null {
  const { modelHealthOpen, setModelHealthOpen, toast } = useApp()
  const [health, setHealth] = useState<ModelHealth | null>(null)
  const [diag, setDiag] = useState<DiagnosticsSnapshot | null>(null)
  const [conformance, setConformance] = useState<Record<string, ProviderConformance>>({})
  const [switches, setSwitches] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)

  const close = (): void => setModelHealthOpen(false)
  const cardRef = useDialogA11y(close, modelHealthOpen)

  const reload = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      const [h, d] = await Promise.all([bridge.model.health(), bridge.diagnostics.snapshot()])
      setHealth(h)
      setDiag(d)
      const results: Record<string, ProviderConformance> = {}
      for (const p of h.providers) {
        try {
          results[p.providerId] = await bridge.model.conformance({ providerId: p.providerId })
        } catch {
          // 单个服务商一致性检查失败不影响整体健康面板
        }
      }
      setConformance(results)
      // 读取回滚开关当前值（未设置时用各能力默认值）
      const flags: Record<string, boolean> = {}
      for (const s of RUNTIME_SWITCHES) {
        const raw = await bridge.settings.get(s.key)
        flags[s.key] = raw === undefined || raw === null ? s.defaultOn : Boolean(raw)
      }
      setSwitches(flags)
    } catch (e) {
      toast(`模型健康读取失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }, [toast])

  useEffect(() => {
    if (modelHealthOpen) void reload()
  }, [modelHealthOpen, reload])

  const toggleSwitch = async (key: string, next: boolean): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      await bridge.settings.set({ key, value: next })
      setSwitches((s) => ({ ...s, [key]: next }))
      toast(`${next ? '已开启' : '已关闭'}：${RUNTIME_SWITCHES.find((x) => x.key === key)?.label ?? key}`)
    } catch (e) {
      toast(`开关更新失败：${errorMessage(e)}`)
    }
  }

  const exportDiagnostics = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const r = await bridge.diagnostics.export()
      if (r) toast(`诊断包已导出：${r.fileName}`)
    } catch (e) {
      toast(`导出失败：${errorMessage(e)}`)
    }
  }

  if (!modelHealthOpen) return null

  const errors = Object.entries(health?.recentErrorCodes ?? {}).sort((a, b) => b[1] - a[1])

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="模型健康"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card recovery-card">
        <div className="modal-head">
          <h3>模型健康</h3>
          <div className="recovery-head-right">
            <span className="audit-hint">不含密钥 · 不含响应正文 · 无外部遥测</span>
            <button className="icon-btn" aria-label="刷新" title="刷新" onClick={() => void reload()}>
              <Icon id="i-new" />
            </button>
            <button className="icon-btn" aria-label="关闭" onClick={close}>
              <Icon id="i-x" />
            </button>
          </div>
        </div>

        <div className="modal-body recovery-body">
          <div className="conv-pane">
            {busy && !health ? (
              <div className="viz-empty">加载中…</div>
            ) : (
              <>
                {(health?.providers ?? []).map((p) => (
                  <div className="cq-row" key={p.providerId}>
                    <div className="cq-main">
                      <div className="cq-badges">
                        <span className="rr-role">{p.label}</span>
                        <span className={`st-badge ${p.configured ? 'st-favorable' : 'st-unfavorable'}`}>
                          {p.configured ? '密钥已配置' : '未配置密钥'}
                        </span>
                        {health?.activeProviderId === p.providerId && (
                          <span className="st-badge st-favorable">当前使用</span>
                        )}
                        <span className="st-badge st-neutral">{p.protocol}</span>
                      </div>
                      <div className="cq-meta">
                        <span className="cq-src none">
                          {p.streaming ? '流式' : '非流式'} · {p.vision ? '支持图片' : '纯文本'} ·{' '}
                          {p.jsonMode ? 'JSON 模式' : '无 JSON 模式'} · 最大 {p.maxTokens} tokens
                        </span>
                        <span className="cq-src none">推理档位 {p.reasoningLevels.join('/')}</span>
                      </div>
                      {conformance[p.providerId] && !conformance[p.providerId].ok && (
                        <div className="cq-text">
                          一致性检查未通过：{conformance[p.providerId].issues.join('；')}
                        </div>
                      )}
                    </div>
                  </div>
                ))}

                <div className="recovery-section" aria-label="最近错误类别">
                  <div className="recovery-section-title">
                    <Icon id="i-alert" />
                    最近错误类别
                  </div>
                  {errors.length === 0 ? (
                    <div className="viz-empty">近期没有失败记录。</div>
                  ) : (
                    errors.map(([code, count]) => (
                      <div className="recovery-sub-row" key={code}>
                        <span className="st-badge st-unfavorable">{code}</span>
                        <span className="recovery-question">{CATEGORY_LABEL[code] ?? '未分类错误'}</span>
                        <span className="cq-src none">{count} 次</span>
                      </div>
                    ))
                  )}
                </div>

                <div className="recovery-section" aria-label="运行时开关">
                  <div className="recovery-section-title">
                    <Icon id="i-toggle" />
                    新能力开关（可独立回滚）
                  </div>
                  {RUNTIME_SWITCHES.map((s) => {
                    const on = switches[s.key] ?? s.defaultOn
                    return (
                      <label className="cap-row" key={s.key} title={on ? s.hint : s.offEffect}>
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={(e) => void toggleSwitch(s.key, e.target.checked)}
                        />
                        <span className="cap-name">{s.label}</span>
                        <span className="cap-risk">{on ? s.hint : s.offEffect}</span>
                      </label>
                    )
                  })}
                  <div className="conv-hint">
                    开关立即生效并持久化；关闭只影响后续行为，<strong>不删除已有数据</strong>。
                    运行时版本由 `agent.runtimeVersion` 钉住，需新建任务才切换。
                  </div>
                </div>

                <div className="recovery-section" aria-label="本地诊断">
                  <div className="recovery-section-title">
                    <Icon id="i-gear" />
                    本地诊断
                  </div>
                  <div className="conv-hint">
                    迁移版本 v{diag?.latestMigration ?? '—'} · Span {diag?.spanCount ?? '—'} 条 · 丢弃{' '}
                    {diag?.droppedSpans ?? 0} 条 · 外部遥测：{diag?.externalTelemetry ? '开启' : '关闭'}
                  </div>
                  <div className="cq-actions">
                    <button className="btn-ghost cq-btn" onClick={() => void exportDiagnostics()}>
                      导出诊断包（本地）
                    </button>
                  </div>
                  <div className="conv-hint">
                    诊断包只含技术字段：迁移版本、Span 计数、错误类别与预算统计；
                    <strong>不含案件原文、查询词、工具参数或密钥</strong>，不可勾选导出敏感字段。
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
