import { useCallback, useEffect, useState, type JSX } from 'react'
import type {
  PluginCapability,
  PluginReview,
  PluginSubsystemStatus,
  PluginSummary
} from '@lawbuddy/contracts'
import { errorMessage, getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { useDialogA11y } from '../hooks/useDialogA11y'
import { Icon } from './Icon'

const CAPABILITY_LABEL: Record<PluginCapability, string> = {
  fs_write: '写入工作空间文件',
  network: '访问网络',
  shell: '执行本机命令',
  case_read: '读取案件材料',
  credential_access: '读取密钥与凭据'
}

const CAPABILITY_RISK: Record<PluginCapability, string> = {
  fs_write: '可修改工作副本与交付物；不会写案件原件',
  network: '案件内容可能离开本机（默认不授予）',
  shell: '等同在本机执行任意命令，风险最高',
  case_read: '可读取当前案件原文（仍受原件只读约束）',
  credential_access: '一律拒绝：插件永远不允许读取密钥'
}

/**
 * 扩展审批（§7.6）：逐项授予高风险能力，不提供「允许全部」。
 *
 * - 默认禁用；启用前必须显式勾选每一项声明的能力；
 * - `credential_access` 在 Core 侧直接拒绝，这里也不提供勾选框；
 * - 审查只读本地 manifest，展示服务器、工具、数据范围与许可证结论。
 */
export function PluginDialog(): JSX.Element | null {
  const { pluginOpen, setPluginOpen, toast } = useApp()
  const [status, setStatus] = useState<PluginSubsystemStatus | null>(null)
  const [plugins, setPlugins] = useState<PluginSummary[]>([])
  const [manifestText, setManifestText] = useState('')
  const [review, setReview] = useState<{ manifest: PluginSummary; review: PluginReview } | null>(null)
  const [selId, setSelId] = useState<string | null>(null)
  const [granted, setGranted] = useState<PluginCapability[]>([])
  const [busy, setBusy] = useState(false)

  const close = (): void => setPluginOpen(false)
  const cardRef = useDialogA11y(close, pluginOpen)

  const reload = useCallback(async () => {
    const bridge = getBridge()
    if (!bridge) return
    try {
      const [st, list] = await Promise.all([bridge.plugin.status(), bridge.plugin.list({ includeDisabled: true })])
      setStatus(st)
      setPlugins(list)
    } catch (e) {
      toast(`扩展状态读取失败：${errorMessage(e)}`)
    }
  }, [toast])

  useEffect(() => {
    if (pluginOpen) void reload()
  }, [pluginOpen, reload])

  const sel = plugins.find((p) => p.pluginId === selId) ?? null

  useEffect(() => {
    // 切换选中插件时清空已勾选能力，避免把 A 的授权误带到 B
    setGranted([])
  }, [selId])

  const doReview = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    if (!manifestText.trim()) {
      toast('请粘贴插件 manifest（JSON）')
      return
    }
    setBusy(true)
    try {
      const r = await bridge.plugin.review({ manifest: manifestText })
      setReview(r)
      toast(r.review.ok ? '审查通过：可安装（仍未启用）' : `审查未通过：${r.review.issues[0] ?? '见详情'}`)
    } catch (e) {
      toast(`审查失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const doInstall = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      const r = await bridge.plugin.install({ manifest: manifestText })
      toast(`已安装（未启用）：${r.name}`)
      setReview(null)
      setManifestText('')
      await reload()
    } catch (e) {
      toast(`安装失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const doEnable = async (): Promise<void> => {
    const bridge = getBridge()
    if (!bridge || !sel) return
    setBusy(true)
    try {
      await bridge.plugin.enable({ pluginId: sel.pluginId, grantedCapabilities: granted })
      toast(`已启用：${sel.name}`)
      await reload()
    } catch (e) {
      toast(`启用失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const doDisable = async (pluginId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      await bridge.plugin.disable({ pluginId })
      toast('已停用并终止其子进程')
      await reload()
    } catch (e) {
      toast(`停用失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const doUninstall = async (pluginId: string): Promise<void> => {
    const bridge = getBridge()
    if (!bridge) return
    setBusy(true)
    try {
      const r = await bridge.plugin.uninstall({ pluginId })
      toast(`已卸载，移除 ${r.toolsRemoved} 个工具（未自动重启）`)
      if (selId === pluginId) setSelId(null)
      await reload()
    } catch (e) {
      toast(`卸载失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }

  if (!pluginOpen) return null

  const declared = (sel?.capabilities ?? {}) as Partial<Record<PluginCapability, boolean>>
  const declaredList = (Object.keys(declared) as PluginCapability[]).filter((c) => declared[c])

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="扩展审批"
      onClick={(e) => {
        if (e.target === e.currentTarget) close()
      }}
    >
      <div ref={cardRef} tabIndex={-1} className="modal-card recovery-card">
        <div className="modal-head">
          <h3>扩展审批</h3>
          <div className="recovery-head-right">
            <span className="audit-hint">默认禁用 · 逐项授权 · 子进程隔离</span>
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
            {status && (
              <div className="recovery-sub-row" aria-label="子系统状态">
                <span className={`st-badge ${status.mcp.enabled ? 'st-favorable' : 'st-neutral'}`}>
                  MCP {status.mcp.enabled ? '已开启' : '已关闭'}
                </span>
                <span className="cq-src none">已登记 {status.mcp.registered}</span>
                <span className="cq-src none">运行中 {status.mcp.running}</span>
                <span className="st-badge st-neutral">插件 {status.plugins.enabled}/{status.plugins.installed}</span>
                <span className="st-badge st-neutral">执行后端 {status.execBackend.active.name}</span>
                {status.execBackend.unimplemented.length > 0 && (
                  <span className="cq-src none">
                    未实现：{status.execBackend.unimplemented.join(' / ')}
                  </span>
                )}
              </div>
            )}

            {plugins.length === 0 ? (
              <div className="viz-empty">尚未安装任何扩展。粘贴 manifest 审查后可安装。</div>
            ) : (
              <div className="plugin-list" role="list">
                {plugins.map((p) => (
                  <button
                    key={p.pluginId}
                    role="listitem"
                    className={`recovery-item${p.pluginId === selId ? ' on' : ''}`}
                    onClick={() => setSelId(p.pluginId)}
                  >
                    <span className="recovery-line">
                      <span className="recovery-mode">{p.name}</span>
                      <span className="st-badge st-neutral">v{p.version}</span>
                      <span className={`st-badge ${p.enabled ? 'st-favorable' : 'st-neutral'}`}>
                        {p.enabled ? '已启用' : '已安装未启用'}
                      </span>
                    </span>
                    <span className="recovery-line recovery-sub">
                      <span>许可证 {p.license}</span>
                      <span>sha:{p.sha256.slice(0, 10)}</span>
                      <span>{p.tools.length} 个工具</span>
                    </span>
                  </button>
                ))}
              </div>
            )}

            {sel && (
              <div className="recovery-section" aria-label="扩展详情">
                <div className="recovery-section-title">
                  <Icon id="i-shield" />
                  授权范围：{sel.name}
                </div>
                <div className="conv-hint">
                  来源：{sel.source || '本地 manifest'} · 工具：{sel.tools.join(' / ') || '无'}
                </div>
                {declaredList.length === 0 ? (
                  <div className="viz-empty">该扩展未声明高风险能力。</div>
                ) : (
                  declaredList.map((cap) => (
                    <label
                      key={cap}
                      className={`cap-row${cap === 'credential_access' ? ' blocked' : ''}`}
                      title={CAPABILITY_RISK[cap]}
                    >
                      <input
                        type="checkbox"
                        checked={cap === 'credential_access' ? false : granted.includes(cap)}
                        disabled={cap === 'credential_access' || sel.enabled}
                        onChange={(e) =>
                          setGranted((g) => (e.target.checked ? [...g, cap] : g.filter((x) => x !== cap)))
                        }
                      />
                      <span className="cap-name">{CAPABILITY_LABEL[cap]}</span>
                      <span className="cap-risk">
                        {cap === 'credential_access' ? '始终拒绝' : CAPABILITY_RISK[cap]}
                      </span>
                    </label>
                  ))
                )}
                <div className="cq-actions">
                  {sel.enabled ? (
                    <>
                      <button className="btn-ghost cq-btn" disabled={busy} onClick={() => void doDisable(sel.pluginId)}>
                        停用
                      </button>
                      <button
                        className="btn-danger cq-btn"
                        disabled={busy}
                        onClick={() => void doUninstall(sel.pluginId)}
                      >
                        卸载
                      </button>
                    </>
                  ) : (
                    <button
                      className="btn-primary cq-btn"
                      disabled={busy || declaredList.some((c) => c !== 'credential_access' && !granted.includes(c))}
                      title={
                        declaredList.some((c) => c !== 'credential_access' && !granted.includes(c))
                          ? '请先逐项勾选该扩展声明的高风险能力'
                          : '启用后其工具才可用'
                      }
                      onClick={() => void doEnable()}
                    >
                      按勾选范围启用
                    </button>
                  )}
                </div>
              </div>
            )}

            <div className="recovery-section" aria-label="安装扩展">
              <div className="recovery-section-title">
                <Icon id="i-plus" />
                审查并安装
              </div>
              <textarea
                className="form-input plugin-manifest"
                value={manifestText}
                placeholder='粘贴 manifest JSON（含 name/version/license/command/tools/capabilities）'
                aria-label="插件 manifest"
                onChange={(e) => setManifestText(e.target.value)}
              />
              <div className="cq-actions">
                <button className="btn-ghost cq-btn" disabled={busy} onClick={() => void doReview()}>
                  只审查
                </button>
                <button
                  className="btn-primary cq-btn"
                  disabled={busy || !review?.review.ok}
                  title={review?.review.ok ? '安装后仍未启用' : '需先通过审查'}
                  onClick={() => void doInstall()}
                >
                  安装（不启用）
                </button>
              </div>
              {review && (
                <div className={`cq-row${review.review.ok ? '' : ' bad'}`}>
                  <div className="cq-main">
                    <div className="cq-badges">
                      <span className="rr-role">{review.manifest.name}</span>
                      <span className={`st-badge ${review.review.ok ? 'st-favorable' : 'st-unfavorable'}`}>
                        {review.review.ok ? '审查通过' : '审查未通过'}
                      </span>
                      <span className={`st-badge ${review.review.licenseAllowed ? 'st-neutral' : 'st-unfavorable'}`}>
                        许可证 {review.manifest.license}
                      </span>
                    </div>
                    {review.review.issues.length > 0 && (
                      <div className="cq-text">拒绝原因：{review.review.issues.join('；')}</div>
                    )}
                    {review.review.warnings.length > 0 && (
                      <div className="cq-text">提示：{review.review.warnings.join('；')}</div>
                    )}
                    {review.review.riskyCapabilities.length > 0 && (
                      <div className="cq-meta">
                        <span className="cq-src none">
                          高风险能力：{review.review.riskyCapabilities.map((c) => CAPABILITY_LABEL[c]).join(' / ')}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
