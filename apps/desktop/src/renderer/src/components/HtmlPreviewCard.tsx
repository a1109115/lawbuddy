import { useEffect, useRef, useState, type JSX } from 'react'
import { createPortal } from 'react-dom'
import { getBridge } from '../services/bridge'
import { useApp } from '../state/store'
import { Icon } from './Icon'

/** 扫描 HTML 中的外部资源引用（src/href/url()/@import 的 http(s) 与协议相对地址） */
function findExternalRefs(html: string): string[] {
  const out = new Set<string>()
  const push = (raw: string): void => {
    try {
      const origin = new URL(raw.startsWith('//') ? `https:${raw}` : raw).origin
      if (origin !== 'null') out.add(origin)
      else out.add(raw.slice(0, 60))
    } catch {
      out.add(raw.slice(0, 60))
    }
  }
  const attrRe = /(?:src|href)\s*=\s*["']?([^"'\s>]+)/gi
  const urlRe = /url\(\s*["']?([^"')\s]+)/gi
  for (const m of html.matchAll(attrRe)) if (/^(https?:)?\/\//i.test(m[1])) push(m[1])
  for (const m of html.matchAll(urlRe)) if (/^(https?:)?\/\//i.test(m[1])) push(m[1])
  return [...out].slice(0, 4)
}

/**
 * HTML 预览卡片：把完整 HTML 文档渲染进沙箱 iframe。
 * 安全模型：iframe sandbox 仅 allow-scripts（无 allow-same-origin，源为透明）；
 * 文档经 lawbuddy-preview:// 自定义协议加载，Main 侧响应头自带宽松 CSP
 * （允许内联脚本/样式，禁一切外部请求、表单与弹窗）。
 * 卡片提供 预览/源码 切换、刷新、复制源码与全屏放大。
 * 双模式：实时会话直传 `html`（组件负责注册换取预览地址）；
 * 历史会话只传 `previewUrl`（Main 磁盘回源渲染，异步取回 HTML 补源码页签）。
 */
export function HtmlPreviewCard({
  html,
  previewUrl,
  onRegistered,
  title = 'HTML 预览'
}: {
  html?: string
  previewUrl?: string
  onRegistered?: (url: string) => void
  title?: string
}): JSX.Element {
  // 恢复模式取回的 HTML（仅补源码页签/外链检测；iframe 直接用 previewUrl）
  const [fetched, setFetched] = useState('')
  const effectiveHtml = html ?? fetched
  const [src, setSrc] = useState(previewUrl && !html ? previewUrl : '')
  const [error, setError] = useState('')
  const [tab, setTab] = useState<'preview' | 'code'>('preview')
  const [full, setFull] = useState(false)
  const [copied, setCopied] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)
  // 图片导出：菜单、进行中格式（PNG/SVG）与沙箱回传请求
  const [exportOpen, setExportOpen] = useState(false)
  const [exporting, setExporting] = useState<'png' | 'svg' | null>(null)
  const pendingExport = useRef<{ id: string; title: string } | null>(null)
  const exportTimer = useRef<number | undefined>(undefined)
  // 外链检测：被沙箱拦截的外部资源可能导致内容空白，明确告知用户而非静默失败
  const blocked = findExternalRefs(effectiveHtml)
  // 诊断：沙箱文档引导脚本（Main 侧注入）上报的脚本错误/加载状态，空白时直接给出原因。
  // 内嵌视图与全屏 Portal 各有一个 iframe 实例，用集合登记、消息按 source 匹配任一实例
  // （单 ref 会在全屏挂载/卸载时被覆盖或置 null，导致诊断失效）
  const framesRef = useRef<Set<HTMLIFrameElement>>(new Set())
  const loadedRef = useRef(false)
  const [diag, setDiag] = useState('')
  const attachFrame = (el: HTMLIFrameElement | null): void => {
    if (el) {
      framesRef.current.add(el)
      return
    }
    // el === null（实例卸载）：惰性清理已断连的 iframe
    for (const f of framesRef.current) {
      if (!f.isConnected) framesRef.current.delete(f)
    }
  }

  // 恢复模式：iframe 直接用 previewUrl 渲染（Main 内存/磁盘回源），异步取回 HTML 补源码页签
  useEffect(() => {
    if (!previewUrl || html) return undefined
    let alive = true
    const id = previewUrl.replace(/^lawbuddy-preview:\/\//, '').replace(/\/+$/, '')
    getBridge()
      ?.preview.fetch(id)
      .then((h) => {
        if (alive && h) setFetched(h)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [previewUrl, html])

  /** 发起图片导出：向已挂载的沙箱 iframe 发请求（沙箱内序列化，结果经 postMessage 回传） */
  const doExport = (format: 'png' | 'svg'): void => {
    setExportOpen(false)
    const frame = [...framesRef.current].find((f) => f.isConnected)
    const view = frame?.contentWindow
    if (!frame || !view) {
      useApp.getState().toast('预览图尚未加载完成，请稍后再导出')
      return
    }
    const id = `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
    pendingExport.current = { id, title }
    setExporting(format)
    window.clearTimeout(exportTimer.current)
    exportTimer.current = window.setTimeout(() => {
      if (pendingExport.current?.id !== id) return
      pendingExport.current = null
      setExporting(null)
      useApp.getState().toast('图片导出超时，请重试（内容较大时可先切到全屏再导出）')
    }, 30000)
    view.postMessage({ __lawbuddy_export__: { id, format, scale: 3 } }, '*')
  }

  /** 沙箱导出结果：PNG 为 base64，SVG 为文本；交给 Main 弹保存对话框落盘 */
  const finishExport = (d: {
    id?: unknown
    ok?: unknown
    format?: unknown
    data?: unknown
    error?: unknown
  }): void => {
    const req = pendingExport.current
    if (!req || String(d.id ?? '') !== req.id) return
    pendingExport.current = null
    window.clearTimeout(exportTimer.current)
    setExporting(null)
    const format = d.format === 'svg' ? 'svg' : 'png'
    const data = typeof d.data === 'string' ? d.data : ''
    if (!d.ok || !data) {
      useApp.getState().toast(`图片导出失败：${String(d.error || '未生成图形数据')}`)
      return
    }
    const bridge = getBridge()
    if (!bridge) return
    void bridge.preview
      .saveImage({
        format,
        title: req.title,
        svg: format === 'svg' ? data : undefined,
        dataBase64: format === 'png' ? data : undefined
      })
      .then((r) => {
        if (r) useApp.getState().toast(`已导出${format === 'svg' ? ' SVG 矢量图' : ' PNG 高清图'}：${r.fileName}`)
      })
      .catch((e: unknown) =>
        useApp.getState().toast(`图片导出失败：${e instanceof Error ? e.message : String(e)}`)
      )
  }

  // html 变化时重新注册到 Main 内存，换取 lawbuddy-preview://<id>；
  // 恢复模式下取回的 HTML 与 previewUrl 内容一致（内容哈希 id），注册结果同址、iframe 不重载
  useEffect(() => {
    if (!effectiveHtml) return undefined
    let alive = true
    setError('')
    const bridge = getBridge()
    if (!bridge) {
      setError('预览桥不可用（非 Electron 环境）')
      return undefined
    }
    bridge.preview
      .register(effectiveHtml)
      .then((u) => {
        if (import.meta.env.DEV) console.info('[htmlpv] 预览地址 =', u)
        if (alive) {
          onRegistered?.(u)
          setSrc(u)
          setTab('preview')
          setReloadKey((k) => k + 1)
        }
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveHtml])

  // 诊断监听：只认引导脚本的标记消息，且 source 必须是已登记的 iframe（防伪造/串扰）。
  // 依赖含 tab：切回预览时 iframe 重挂载重载文档，诊断周期重新武装（清空旧结论、重设超时）
  useEffect(() => {
    if (!src || tab !== 'preview') return undefined
    setDiag('')
    loadedRef.current = false
    const onMsg = (e: MessageEvent): void => {
      const d = e.data as {
        __lawbuddy_preview__?: string
        message?: string
        id?: unknown
        ok?: unknown
        format?: unknown
        data?: unknown
        error?: unknown
      } | null
      if (!d || typeof d.__lawbuddy_preview__ !== 'string') return
      const known = [...framesRef.current].some((f) => f.contentWindow === e.source)
      if (!known) return
      if (d.__lawbuddy_preview__ === 'export') {
        finishExport(d)
        return
      }
      const msg = typeof d.message === 'string' ? d.message : ''
      if (d.__lawbuddy_preview__ === 'error') setDiag(msg || '未知脚本错误')
      else if (d.__lawbuddy_preview__ === 'loaded') {
        loadedRef.current = true
        if (msg === '0') setDiag((p) => p || '文档已加载，但 <body> 没有可见内容')
      }
    }
    // 加载超时：协议请求没发生或文档没送达（配合开发终端 [preview] 日志定位）
    const timer = window.setTimeout(() => {
      if (!loadedRef.current) setDiag((p) => p || 'iframe 5 秒内未完成加载（文档可能未送达，请重启应用后重试）')
    }, 5000)
    window.addEventListener('message', onMsg)
    return () => {
      window.removeEventListener('message', onMsg)
      window.clearTimeout(timer)
    }
  }, [src, reloadKey, tab])

  // 全屏时 Esc 退出
  useEffect(() => {
    if (!full) return undefined
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setFull(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [full])

  // 导出菜单：点击外部关闭（菜单按钮自身阻止冒泡）
  useEffect(() => {
    if (!exportOpen) return undefined
    const close = (): void => setExportOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [exportOpen])

  // 卸载时清理导出超时计时器，避免对已卸载组件更新状态
  useEffect(() => () => window.clearTimeout(exportTimer.current), [])

  const copyCode = (): void => {
    if (!effectiveHtml) return
    void navigator.clipboard.writeText(effectiveHtml).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  // 卡片内容同时用于内嵌视图与全屏 Portal，抽成共享片段
  const inner = (
    <>
      <div className="htmlpv-head">
        <span className="htmlpv-title">
          <Icon id="i-chart" />
          {title}
        </span>
        <div className="htmlpv-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'preview'}
            className={tab === 'preview' ? 'on' : ''}
            onClick={() => setTab('preview')}
          >
            预览
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'code'}
            className={tab === 'code' ? 'on' : ''}
            onClick={() => setTab('code')}
          >
            源码
          </button>
        </div>
        <div className="htmlpv-acts">
          {tab === 'preview' && src && (
            <button type="button" className="htmlpv-btn" title="重新加载" onClick={() => setReloadKey((k) => k + 1)}>
              <Icon id="i-rotate" />
            </button>
          )}
          {tab === 'preview' && src && (
            <div className="htmlpv-export">
              <button
                type="button"
                className="htmlpv-btn"
                title="导出图片（PNG 高清 / SVG 矢量）"
                aria-haspopup="menu"
                aria-expanded={exportOpen}
                disabled={exporting !== null}
                onClick={(e) => {
                  e.stopPropagation()
                  setExportOpen((v) => !v)
                }}
              >
                {exporting ? <span className="task-spin" aria-hidden="true" /> : <Icon id="i-download" />}
              </button>
              {exportOpen && (
                <div className="htmlpv-menu" role="menu">
                  <button
                    type="button"
                    role="menuitem"
                    onClick={(e) => {
                      e.stopPropagation()
                      doExport('png')
                    }}
                  >
                    PNG 高清图
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={(e) => {
                      e.stopPropagation()
                      doExport('svg')
                    }}
                  >
                    SVG 矢量图
                  </button>
                </div>
              )}
            </div>
          )}
          <button type="button" className="htmlpv-btn" title="复制源码" onClick={copyCode}>
            <Icon id={copied ? 'i-check' : 'i-copy'} />
          </button>
          <button
            type="button"
            className="htmlpv-btn"
            title={full ? '退出全屏（Esc）' : '全屏'}
            onClick={() => setFull((v) => !v)}
          >
            <Icon id={full ? 'i-shrink' : 'i-expand'} />
          </button>
        </div>
      </div>
      <div className="htmlpv-body">
        {error ? (
          <div className="htmlpv-err">
            <Icon id="i-alert" />
            {error}
          </div>
        ) : tab === 'preview' ? (
          src ? (
            <div className="htmlpv-preview">
              {(diag || blocked.length > 0) && (
                <div className="htmlpv-warn" role="alert">
                  <Icon id="i-alert" />
                  <span>
                    {diag
                      ? `预览诊断：${diag}`
                      : `沙箱已拦截外部资源（${blocked.join('、')}），内容可能显示不全；预览仅支持内联样式与脚本。`}
                  </span>
                </div>
              )}
              {/* 沙箱 iframe：仅允许脚本，不给同源权限；重新加载靠 key 重挂载 */}
              <iframe
                key={reloadKey}
                ref={attachFrame}
                className="htmlpv-frame"
                src={src}
                sandbox="allow-scripts"
                referrerPolicy="no-referrer"
                title={title}
              />
            </div>
          ) : (
            <div className="htmlpv-loading">正在准备预览…</div>
          )
        ) : (
          effectiveHtml ? (
            <pre className="htmlpv-code">{effectiveHtml}</pre>
          ) : (
            <div className="htmlpv-loading">源码不可用（预览内容已被清理，预览仍可查看）</div>
          )
        )}
      </div>
    </>
  )

  return (
    <>
      <div className="htmlpv">{inner}</div>
      {/* 真全屏：整卡 Portal 到 body。消息气泡等祖先的 transform 会创建 containing block
          困住 fixed 定位（表现为只铺满中间聊天列），挂到 body 才能铺满整个应用窗口；
          全屏层用独立 iframe 实例，原卡片保持挂载、沙箱内状态不丢 */}
      {full && createPortal(<div className="htmlpv htmlpv-fs">{inner}</div>, document.body)}
    </>
  )
}
