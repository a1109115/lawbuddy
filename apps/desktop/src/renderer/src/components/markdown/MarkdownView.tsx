import { Fragment, useEffect, useRef, useState, type CSSProperties, type JSX, type ReactNode, type RefObject } from 'react'

/**
 * 共享 Markdown 渲染（markdown-it token 树 → React 元素）。
 *
 * 解析层 `html: false`——原始标签按文本转义；渲染层直接构造 React 元素，
 * 不产出 HTML 字符串，不存在原始 HTML 注入点（S-014）。
 * 链接只放行 http/https/mailto/#，其余协议退化为纯文本。
 *
 * 消费方：
 * - 原件预览（preview/OriginalPreview.tsx）：链接点击一律阻止（不触发应用内导航）
 * - Agent 聊天气泡（ChatView.tsx）：https 外链经 Main 白名单打开系统浏览器
 */

/** markdown-it token 的最小结构（只取渲染需要的字段） */
interface MdToken {
  type: string
  tag: string
  nesting: number
  content: string
  hidden?: boolean
  info?: string
  attrs?: [string, string][] | null
  children?: MdToken[] | null
}

interface MdParser {
  // markdown-it 15 的 parse 不会补默认 env：正文含 `[`（如「[2005]民立他字第8号复函」）
  // 会进入引用式链接规则并读取 state.env.references，env 为 undefined 即抛错。
  // 因此每次解析都显式传入独立 env 对象（引用定义按文档隔离，不跨次累积）
  parse: (src: string, env: Record<string, unknown>) => MdToken[]
}

let mdParser: MdParser | null = null

/** 惰性创建 markdown-it（缓存后二次解析为同步开销） */
async function parseMarkdown(source: string): Promise<MdToken[]> {
  if (!mdParser) {
    const mod = await import('markdown-it')
    mdParser = new mod.default({ html: false, linkify: true, breaks: false }) as unknown as MdParser
  }
  return mdParser.parse(source, {})
}

/** 容器 token（nesting=1）在 token 流中的配对收尾下标 */
function findClose(tokens: MdToken[], start: number): number {
  let depth = 0
  for (let i = start; i < tokens.length; i++) {
    if (tokens[i].nesting === 1) depth++
    else if (tokens[i].nesting === -1) {
      depth--
      if (depth === 0) return i
    }
  }
  return tokens.length - 1
}

function attrOf(t: MdToken, name: string): string | undefined {
  return t.attrs?.find((a) => a[0] === name)?.[1]
}

/** 链接只放行常规协议；其余（含脚本协议）退化为纯文本 */
function safeHref(href: string): string | undefined {
  return /^(https?:|mailto:|#)/i.test(href) ? href : undefined
}

/** 引用渲染器：命中《文档名》[第N页|第N-M页|无页码] 时由调用方注入角标元素（ChatView 引用角标）。页码缺失为 null */
export type CiteRenderer = (title: string, pageNo: number | null, pageEnd: number | null) => ReactNode

/**
 * HTML 代码块渲染器：语言标注为 html 的 fence 交给调用方（方案 A 自动预览卡）。
 * 仅当调用方显式传入时启用；未传入时照常按代码块渲染（原件预览不受影响）。
 */
export type HtmlBlockRenderer = (html: string, key: string) => ReactNode

/** fence 语言标注是否为 html（```html / ```HTML / ```html title=xx） */
function isHtmlFence(t: MdToken): boolean {
  return (t.info ?? '').trim().toLowerCase().split(/\s+/)[0] === 'html'
}

/** 渲染环境：引用渲染器 ref + HTML 块渲染器（递归透传，避免逐层穿参） */
interface RenderEnv {
  /** 经 ref 间接引用：角标数据（answer_verification）晚于文本到达时，重渲染即可补上 */
  citeRef?: RefObject<CiteRenderer | undefined>
  html?: HtmlBlockRenderer
}

/**
 * 引用形态：《文档名》第N页 / 第N-M页 / 仅《文档名》（后端 verify 同步识别三种形态；
 * 无页码引用是否渲染成角标由渲染器决定——法条/书名等非案件文档保持原文）。
 */
const CITE_RE = /《([^《》\n]{1,80})》(?:\s*第\s*(\d{1,4})(?:\s*[-—~至]\s*(\d{1,4}))?\s*页)?/g

/**
 * 惰性引用角标：React 渲染期才读取当前渲染器（而非 token 树构建时固化结果）。
 * 渲染器未就绪（校验数据未到）或判定保持原文时回退为原始引用文本，避免正文被吞或产生空括号。
 */
function LazyCite({
  citeRef,
  title,
  pageNo,
  pageEnd,
  raw
}: {
  citeRef: RefObject<CiteRenderer | undefined>
  title: string
  pageNo: number | null
  pageEnd: number | null
  /** 正则命中的原始片段（渲染器未就绪/保持原文时原样回退，不改写原文空格） */
  raw: string
}): JSX.Element {
  const node = citeRef.current?.(title, pageNo, pageEnd)
  return <>{node ?? raw}</>
}

/** 文本中的引用 → 角标与普通文本交错（无渲染器 ref 时原样返回单个字符串） */
function renderTextWithCites(content: string, env: RenderEnv, keyBase: string): ReactNode[] {
  if (!env.citeRef || !content.includes('《')) return [content]
  const out: ReactNode[] = []
  let last = 0
  let idx = 0
  CITE_RE.lastIndex = 0
  for (let m = CITE_RE.exec(content); m !== null; m = CITE_RE.exec(content)) {
    if (m.index > last) out.push(content.slice(last, m.index))
    out.push(
      <LazyCite
        key={`${keyBase}-cite-${idx++}`}
        citeRef={env.citeRef}
        title={m[1].trim()}
        pageNo={m[2] ? Number(m[2]) : null}
        pageEnd={m[3] ? Number(m[3]) : null}
        raw={m[0]}
      />
    )
    last = m.index + m[0].length
  }
  if (last < content.length) out.push(content.slice(last))
  return out
}

/** 表格单元格对齐（markdown-it 通过 style 属性给出） */
function alignOf(t: MdToken): CSSProperties | undefined {
  const m = attrOf(t, 'style')?.match(/text-align:\s*(left|center|right)/i)
  return m ? { textAlign: m[1].toLowerCase() as CSSProperties['textAlign'] } : undefined
}

/** 图片只加载内联/本地资源；外链图片由 CSP（img-src 'self' data: blob:）拦截 */
function inlineImage(t: MdToken, key: string): ReactNode {
  const src = attrOf(t, 'src') ?? ''
  const alt = t.content || attrOf(t, 'alt') || ''
  if (!/^(https?:|data:|blob:)/i.test(src)) {
    return (
      <span key={key} className="md-img-alt">
        [图片] {alt || src}
      </span>
    )
  }
  return <img key={key} src={src} alt={alt} title={attrOf(t, 'title')} />
}

/** 行内 token → React 元素 */
function renderInline(tokens: MdToken[], env: RenderEnv = {}): ReactNode[] {
  const out: ReactNode[] = []
  let i = 0
  while (i < tokens.length) {
    const t = tokens[i]
    const key = `${t.type}-${i}`
    switch (t.type) {
      case 'inline':
        out.push(<Fragment key={key}>{renderInline(t.children ?? [], env)}</Fragment>)
        break
      case 'text':
        out.push(...renderTextWithCites(t.content, env, key))
        break
      case 'softbreak':
        out.push(' ')
        break
      case 'hardbreak':
        out.push(<br key={key} />)
        break
      case 'code_inline':
        out.push(<code key={key}>{t.content}</code>)
        break
      case 'image':
        out.push(inlineImage(t, key))
        break
      case 'link_open': {
        const close = findClose(tokens, i)
        const body = renderInline(tokens.slice(i + 1, close), env)
        const href = safeHref(attrOf(t, 'href') ?? '')
        out.push(
          href ? (
            <a key={key} href={href} title={attrOf(t, 'title')}>
              {body}
            </a>
          ) : (
            <span key={key}>{body}</span>
          )
        )
        i = close
        break
      }
      case 'strong_open':
      case 'em_open':
      case 's_open': {
        const close = findClose(tokens, i)
        const body = renderInline(tokens.slice(i + 1, close), env)
        if (t.type === 'strong_open') out.push(<strong key={key}>{body}</strong>)
        else if (t.type === 'em_open') out.push(<em key={key}>{body}</em>)
        else out.push(<s key={key}>{body}</s>)
        i = close
        break
      }
      default:
        break
    }
    i++
  }
  return out
}

/** 块级 token → React 元素 */
function renderBlocks(tokens: MdToken[], env: RenderEnv = {}): ReactNode[] {
  const out: ReactNode[] = []
  let i = 0
  while (i < tokens.length) {
    const t = tokens[i]
    const key = `${t.type}-${i}`
    if (t.nesting === 1) {
      const close = findClose(tokens, i)
      out.push(renderBlock(t, tokens.slice(i + 1, close), key, env))
      i = close + 1
      continue
    }
    if (t.type === 'fence' || t.type === 'code_block') {
      // ```html 代码块：调用方提供渲染器（如预览卡）时交给它，否则照常显示源码
      if (t.type === 'fence' && env.html && isHtmlFence(t) && t.content.trim()) {
        out.push(env.html(t.content, key))
      } else {
        out.push(<pre key={key}><code>{t.content}</code></pre>)
      }
    }
    else if (t.type === 'hr') out.push(<hr key={key} />)
    else if (t.type === 'inline') out.push(<Fragment key={key}>{renderInline(t.children ?? [], env)}</Fragment>)
    i++
  }
  return out
}

function renderBlock(t: MdToken, inner: MdToken[], key: string, env: RenderEnv = {}): ReactNode {
  switch (t.type) {
    case 'paragraph_open':
      // 紧凑列表中的段落不再额外包一层 <p>
      return t.hidden ? <Fragment key={key}>{renderBlocks(inner, env)}</Fragment> : <p key={key}>{renderBlocks(inner, env)}</p>
    case 'heading_open': {
      const body = renderInline(inner, env)
      if (t.tag === 'h1') return <h1 key={key}>{body}</h1>
      if (t.tag === 'h2') return <h2 key={key}>{body}</h2>
      if (t.tag === 'h3') return <h3 key={key}>{body}</h3>
      if (t.tag === 'h4') return <h4 key={key}>{body}</h4>
      if (t.tag === 'h5') return <h5 key={key}>{body}</h5>
      return <h6 key={key}>{body}</h6>
    }
    case 'blockquote_open':
      return <blockquote key={key}>{renderBlocks(inner, env)}</blockquote>
    case 'bullet_list_open':
      return <ul key={key}>{renderBlocks(inner, env)}</ul>
    case 'ordered_list_open':
      return <ol key={key}>{renderBlocks(inner, env)}</ol>
    case 'list_item_open':
      return <li key={key}>{renderBlocks(inner, env)}</li>
    case 'table_open':
      // 包一层滚动容器：表格本体 width:100% 随容器自适应，超宽时容器横向滚动
      return (
        <div key={key} className="md-table-wrap">
          <table>{renderBlocks(inner, env)}</table>
        </div>
      )
    case 'thead_open':
      return <thead key={key}>{renderBlocks(inner, env)}</thead>
    case 'tbody_open':
      return <tbody key={key}>{renderBlocks(inner, env)}</tbody>
    case 'tr_open':
      return <tr key={key}>{renderBlocks(inner, env)}</tr>
    case 'th_open':
      return (
        <th key={key} style={alignOf(t)}>
          {renderBlocks(inner, env)}
        </th>
      )
    case 'td_open':
      return (
        <td key={key} style={alignOf(t)}>
          {renderBlocks(inner, env)}
        </td>
      )
    default:
      return <Fragment key={key}>{renderBlocks(inner, env)}</Fragment>
  }
}

/**
 * Markdown 渲染组件：source 变化时异步解析为 token 树，元素树在渲染期构造。
 *
 * - className：渲染容器类名（如预览的 md-body / 聊天的 chat-md，控制字体与间距）
 * - onLinkClick：链接点击回调；缺省时阻止默认导航（原件预览行为）
 * - renderCitation：引用角标渲染器（命中《文档名》第N页时注入；聊天引用角标用）。
 *   经 ref 间接调用：identity 变化不触发重新解析，但渲染器从无到有/数据更新时
 *   随父组件重渲染由 LazyCite 补显角标（不缓存元素树，避免 React 子树跳过渲染）；
 * - renderHtmlBlock：```html 代码块渲染器（如预览卡）；缺省时照常显示源码。
 *   同样经 ref 间接调用；
 * - 解析完成前先以纯文本占位（markdown-it 首次动态导入后缓存，占位极短）
 */
export function MarkdownView({
  source,
  className,
  onLinkClick,
  renderCitation,
  renderHtmlBlock
}: {
  source: string
  className?: string
  onLinkClick?: (href: string) => void
  renderCitation?: CiteRenderer
  renderHtmlBlock?: HtmlBlockRenderer
}): JSX.Element {
  // 只缓存 token 树；React 元素在渲染期现算。
  // 若把元素树存进 state，角标数据（answer_verification）晚到时父组件重渲染
  // 会因元素引用不变被 React 跳过子树重渲染，LazyCite 读不到新 ref → 角标永远不补显。
  const [tokens, setTokens] = useState<MdToken[] | null>(null)
  const citeRef = useRef<CiteRenderer | undefined>(renderCitation)
  citeRef.current = renderCitation
  const htmlRef = useRef<HtmlBlockRenderer | undefined>(renderHtmlBlock)
  htmlRef.current = renderHtmlBlock

  useEffect(() => {
    let cancelled = false
    setTokens(null)
    void (async () => {
      try {
        const t = await parseMarkdown(source)
        if (!cancelled) setTokens(t)
      } catch {
        if (!cancelled) setTokens(null) // 解析失败：回退纯文本
      }
    })()
    return () => {
      cancelled = true
    }
  }, [source])

  // 渲染期构造元素树：LazyCite 此时读取 citeRef.current，
  // 角标数据到达后随父组件重渲染自动从灰角标/原文升级为带校验状态的角标
  const stableHtml: HtmlBlockRenderer = (html, key) => htmlRef.current?.(html, key) ?? null

  return (
    <div
      className={className}
      onClick={(e) => {
        const a = (e.target as HTMLElement).closest('a')
        if (!a) return
        if (onLinkClick) {
          e.preventDefault()
          onLinkClick(a.getAttribute('href') ?? '')
        } else {
          // 缺省：阻止应用内导航（外链在 Main 侧还有白名单兜底）
          e.preventDefault()
        }
      }}
    >
      {tokens ? renderBlocks(tokens, { citeRef, html: stableHtml }) : source}
    </div>
  )
}
