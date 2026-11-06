import { app, BrowserWindow, clipboard, dialog, ipcMain, protocol, session, shell } from 'electron'
import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, readdir, stat, unlink, writeFile } from 'node:fs/promises'
import { existsSync, readFileSync, renameSync, writeFileSync, type Dirent } from 'node:fs'
import { basename, dirname, extname, isAbsolute, join } from 'node:path'
import type {
  ArtifactExportFormat,
  ImportOutcome,
  SaveProviderRequest,
  WorkspaceInfo
} from '@lawbuddy/contracts'
import { CoreProcess } from './coreProcess'

const isDev = !!process.env.ELECTRON_RENDERER_URL

if (isDev) {
  // 开发期远程调试端口：Playwright/CDP 可连接 renderer 查看控制台
  app.commandLine.appendSwitch('remote-debugging-port', '9223')
}

let core: CoreProcess | null = null
let mainWindow: BrowserWindow | null = null

// ---- HTML 预览：lawbuddy-preview:// 自定义协议 --------------------------------
// srcdoc/blob 文档会继承父页 CSP（生产 script-src 'self' 会拦内联脚本），
// 因此预览 HTML 走自定义协议：Main 内存按 ID 存 HTML，响应头自带宽松 CSP，
// iframe 以 sandbox="allow-scripts"（无 allow-same-origin）加载，脚本可用且与主页面隔离。
const PREVIEW_SCHEME = 'lawbuddy-preview'
// HTML 预览上限：2MB（覆盖大多数报告类 HTML；超大文档提示用户改用原文件打开）
const PREVIEW_MAX_BYTES = 2_000_000
const PREVIEW_MAX_ENTRIES = 50
// 图形导出上限（SVG/PNG 字节数）
const GRAPHIC_MAX_BYTES = 32 * 1024 * 1024
// 磁盘持久化：历史会话恢复后，内存淘汰/应用重启仍可回源加载预览
const PREVIEW_DISK_MAX = 200
const previewStore = new Map<string, string>()

// ---- 音视频预览：lawbuddy-media:// 自定义协议（Range 流式读取） ----------------
// Renderer 只在 <audio>/<video> 里使用 lawbuddy-media://<contentId>，不带任何文件路径；
// Main 按 HTTP Range 向 Core 内容会话读取指定字节并返回 206，支持拖动进度条，
// 避免把整段录像 base64 一次性塞进渲染进程。会话关闭即失效。
const MEDIA_SCHEME = 'lawbuddy-media'
const MEDIA_MAX_CHUNK = 4 * 1024 * 1024
const mediaSessions = new Map<string, { mime: string; size: number }>()

/** 登记已打开的内容会话元数据，供 lawbuddy-media:// 协议按 Range 读取（非媒体类型也登记，无副作用） */
function rememberMediaSession(info: { contentId?: string; mime?: string; sizeBytes?: number } | null): void {
  if (!info?.contentId) return
  mediaSessions.set(info.contentId, {
    mime: info.mime || 'application/octet-stream',
    size: Number(info.sizeBytes) || 0
  })
}

const previewDir = (): string => join(app.getPath('userData'), 'previews')

/** 内存 LRU 淘汰（磁盘是第二层，淘汰不影响历史恢复） */
function evictPreviewLru(): void {
  while (previewStore.size >= PREVIEW_MAX_ENTRIES) {
    const oldest = previewStore.keys().next().value
    if (oldest === undefined) break
    previewStore.delete(oldest)
  }
}

/** 内存查无时从磁盘回源（文件名 = 内容哈希 id） */
async function loadPreviewFromDisk(id: string): Promise<string> {
  try {
    return await readFile(join(previewDir(), `${id}.html`), 'utf-8')
  } catch {
    return ''
  }
}

/** 启动清理：磁盘预览文件超过上限时按修改时间删最旧，防无限膨胀 */
async function prunePreviewDisk(): Promise<void> {
  try {
    const names = (await readdir(previewDir())).filter((n) => n.endsWith('.html'))
    if (names.length <= PREVIEW_DISK_MAX) return
    const items = await Promise.all(
      names.map(async (n) => ({ n, t: (await stat(join(previewDir(), n))).mtimeMs }))
    )
    items.sort((a, b) => a.t - b.t)
    for (const f of items.slice(0, items.length - PREVIEW_DISK_MAX)) {
      await unlink(join(previewDir(), f.n)).catch(() => {})
    }
  } catch {
    // 目录不存在等：无需清理
  }
}

// 必须在 app ready 前注册；standard 让 <id> 落在 host 段，secure 让文档处于安全上下文
protocol.registerSchemesAsPrivileged([
  { scheme: PREVIEW_SCHEME, privileges: { standard: true, secure: true } },
  {
    scheme: MEDIA_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      stream: true,
      supportFetchAPI: true,
      corsEnabled: true
    }
  }
])

// 预览文档引导脚本：向父页上报加载完成与脚本/资源错误，卡片据此把「空白」变成可解释状态
const PREVIEW_BOOTSTRAP =
  '<script>(function(){var s=function(t,m){try{parent.postMessage({__lawbuddy_preview__:t,message:String(m||"")},"*")}catch(e){}};' +
  'window.addEventListener("error",function(e){if(e&&e.message)s("error",e.message+(e.lineno?" (行 "+e.lineno+")":""));' +
  'else if(e&&e.target&&e.target!==window&&e.target.tagName)s("error","资源加载失败: <"+e.target.tagName.toLowerCase()+"> "+String(e.target.src||e.target.href||"").slice(0,80))},true);' +
  'window.addEventListener("unhandledrejection",function(e){var r=e&&e.reason;s("error","Promise 拒绝: "+((r&&r.message)||r))});' +
  'window.addEventListener("load",function(){s("loaded",document.body?String(document.body.children.length):"0")})})()</script>'

// 预览文档导出脚本：父页发 {__lawbuddy_export__:{id,format,scale}}，沙箱内把真实渲染结果
// 序列化为图片回传（iframe 无 allow-same-origin，父页无法直接读取其 DOM）。
// - 纯 SVG 图表：直接序列化 SVG 并内联文档 CSS（真正的矢量图，打印/插入 Word 清晰）；
// - HTML 版式图：克隆 DOM 并内联各元素计算样式，以 <foreignObject> 包进 SVG（Chromium 可渲染）；
// - PNG：把上面的 SVG 用页面内 canvas 以 2~3 倍分辨率栅格化，输出高清位图。
// 模型生成的页面内容不可信：序列化前剔除 script/iframe/object/embed 与 on* 属性。
const PREVIEW_EXPORT_BOOTSTRAP = `<script>(function(){
var REQ='__lawbuddy_export__';
function post(p){try{parent.postMessage(Object.assign({__lawbuddy_preview__:'export'},p),'*')}catch(e){}}
function cssText(){var out=[];for(var i=0;i<document.styleSheets.length;i++){try{var rs=document.styleSheets[i].cssRules;for(var j=0;j<rs.length;j++){out.push(rs[j].cssText)}}catch(e){}}return out.join('\\n')}
function unsafe(n){var t=(n.tagName||'').toLowerCase();return t==='script'||t==='iframe'||t==='object'||t==='embed'}
function strip(root){var all=[root].concat([].slice.call(root.querySelectorAll?root.querySelectorAll('*'):[]));for(var i=all.length-1;i>=0;i--){var n=all[i];if(!n.tagName)continue;if(unsafe(n)){if(n.parentNode)n.parentNode.removeChild(n);continue}if(n.attributes){for(var k=n.attributes.length-1;k>=0;k--){var a=n.attributes[k];if(/^on/i.test(a.name))n.removeAttribute(a.name)}}}}
function area(el){var r=el.getBoundingClientRect();return Math.max(0,r.width)*Math.max(0,r.height)}
function copyStyle(src,dst){var cs=getComputedStyle(src);for(var i=0;i<cs.length;i++){var p=cs[i],v=cs.getPropertyValue(p);if(v)dst.style.setProperty(p,v,cs.getPropertyPriority(p))}}
function cloneStyled(el){
if(el.nodeType===3)return document.createTextNode(el.nodeValue||'');
if(el.nodeType!==1)return null;
var tag=(el.tagName||'').toLowerCase();
if(tag==='canvas'){var img=document.createElement('img');try{img.src=el.toDataURL('image/png')}catch(e){}var ir=el.getBoundingClientRect();copyStyle(el,img);img.style.width=Math.max(1,Math.round(ir.width))+'px';img.style.height=Math.max(1,Math.round(ir.height))+'px';return img}
var out=el.cloneNode(false);copyStyle(el,out);
for(var c=el.firstChild;c;c=c.nextSibling){var k=cloneStyled(c);if(k)out.appendChild(k)}
return out}
function svgOf(el){
var r=el.getBoundingClientRect(),w=Math.max(1,Math.round(r.width)),h=Math.max(1,Math.round(r.height));
var out=el.cloneNode(true);strip(out);
var bg=document.createElementNS('http://www.w3.org/2000/svg','rect');bg.setAttribute('x','0');bg.setAttribute('y','0');bg.setAttribute('width','100%');bg.setAttribute('height','100%');bg.setAttribute('fill','#ffffff');
var st=document.createElementNS('http://www.w3.org/2000/svg','style');st.textContent=cssText();
out.insertBefore(st,out.firstChild);out.insertBefore(bg,out.firstChild);
out.setAttribute('xmlns','http://www.w3.org/2000/svg');out.setAttribute('xmlns:xlink','http://www.w3.org/1999/xlink');
out.setAttribute('width',String(w));out.setAttribute('height',String(h));
out.style.width=w+'px';out.style.height=h+'px';
if(!out.getAttribute('viewBox'))out.setAttribute('viewBox','0 0 '+w+' '+h);
return{svg:new XMLSerializer().serializeToString(out),width:w,height:h}}
function htmlOf(){
var b=document.body,kids=[],x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
for(var c=b.firstElementChild;c;c=c.nextElementSibling){
var cs=getComputedStyle(c);
if(cs.display==='none'||cs.visibility==='hidden')continue;
var r=c.getBoundingClientRect();
if(r.width<=0&&r.height<=0)continue;
kids.push(c);x0=Math.min(x0,r.left);y0=Math.min(y0,r.top);x1=Math.max(x1,r.right);y1=Math.max(y1,r.bottom)
}
if(!kids.length){var br=b.getBoundingClientRect();x0=br.left;y0=br.top;x1=br.right;y1=br.bottom;for(var i=0;i<b.childNodes.length;i++)kids.push(b.childNodes[i])}
var pad=8,w=Math.max(1,Math.ceil(x1-x0+pad*2)),h=Math.max(1,Math.ceil(y1-y0+pad*2));
var wrap=document.createElement('div');wrap.setAttribute('xmlns','http://www.w3.org/1999/xhtml');
wrap.style.width=w+'px';wrap.style.height=h+'px';wrap.style.padding=pad+'px';wrap.style.boxSizing='border-box';
wrap.style.position='relative';wrap.style.background=getComputedStyle(b).backgroundColor||'#fff';
wrap.style.fontFamily=getComputedStyle(b).fontFamily;
for(var j=0;j<kids.length;j++){var k=kids[j];if(k.nodeType===1)wrap.appendChild(cloneStyled(k));else if(k.nodeType===3&&(k.nodeValue||'').trim())wrap.appendChild(document.createTextNode(k.nodeValue))}
strip(wrap);
return{svg:'<svg xmlns="http://www.w3.org/2000/svg" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'"><foreignObject x="0" y="0" width="100%" height="100%">'+new XMLSerializer().serializeToString(wrap)+'</foreignObject></svg>',width:w,height:h}}
function pickSvg(){
if(!document.body)return null;
var list=[].slice.call(document.querySelectorAll('svg')).filter(function(s){var r=s.getBoundingClientRect();return r.width>60&&r.height>40});
if(!list.length)return null;
list.sort(function(a,b){return area(b)-area(a)});
var best=list[0];
// SVG 外若还有可见文字，说明 SVG 只是整幅版式的一部分（如 HTML 卡片 + SVG 连线），
// 直接序列化会丢内容——改走整页 HTML→foreignObject 的导出路径。
var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),t;
while((t=w.nextNode())){
var n=t.nodeValue||'';if(!n.trim())continue;
var p=t.parentElement;if(!p||best.contains(p))continue;
var r=p.getBoundingClientRect();if(r.width>0&&r.height>0)return null
}
var ba=Math.max(1,area(document.body));
return(list.length===1||area(best)>=ba*0.35)?best:null}
function toPng(o,scale){
return new Promise(function(resolve,reject){
var s=Math.max(1,Math.min(Number(scale)||2,8192/Math.max(o.width,o.height)));
var img=new Image();
img.onload=function(){try{var cv=document.createElement('canvas');cv.width=Math.max(1,Math.round(o.width*s));cv.height=Math.max(1,Math.round(o.height*s));var ctx=cv.getContext('2d');if(!ctx)throw new Error('画布不可用');ctx.fillStyle='#ffffff';ctx.fillRect(0,0,cv.width,cv.height);ctx.drawImage(img,0,0,cv.width,cv.height);resolve(cv.toDataURL('image/png').split(',')[1]||'')}catch(e){reject(e)}};
img.onerror=function(){reject(new Error('图形栅格化失败'))};
img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(o.svg)})}
window.addEventListener('message',function(ev){
var d=ev.data;if(!d||!d[REQ])return;
var q=d[REQ],id=String(q.id||'');
try{
var best=pickSvg(),o=best?svgOf(best):htmlOf();
if(q.format==='svg'){post({id:id,ok:true,format:'svg',data:o.svg})}
else if(q.format==='png'){toPng(o,q.scale).then(function(b){post({id:id,ok:true,format:'png',data:b})},function(e){post({id:id,ok:false,error:String(e&&e.message||e)})})}
else{post({id:id,ok:false,error:'不支持的格式'})}
}catch(e){post({id:id,ok:false,error:String(e&&e.message||e)})}
})
})()</script>`

function injectBootstrap(html: string): string {
  // 注入点：紧跟 <head>/<html> 开标签之后；无结构时前置兜底
  const script = PREVIEW_BOOTSTRAP + PREVIEW_EXPORT_BOOTSTRAP
  if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, (m) => m + script)
  if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, (m) => m + script)
  return script + html
}

function registerPreviewProtocol(): void {
  protocol.handle(PREVIEW_SCHEME, async (request) => {
    const id = new URL(request.url).host
    let html = (id && previewStore.get(id)) || ''
    if (!html && id) {
      // 内存淘汰/应用重启后回源磁盘，恢复历史会话预览
      html = await loadPreviewFromDisk(id)
      if (html) {
        evictPreviewLru()
        previewStore.set(id, html)
      }
    }
    if (!html) {
      if (isDev) console.log(`[preview] miss: ${id}`)
      return new Response('预览内容已失效，请重新生成', { status: 404 })
    }
    if (isDev) console.log(`[preview] serve: ${id} (${html.length} chars)`)
    // 沙箱内文档自身的 CSP：允许内联脚本/样式，禁一切外部资源与表单/弹窗
    return new Response(injectBootstrap(html), {
      status: 200,
      headers: {
        'Content-Type': 'text/html; charset=utf-8',
        'Content-Security-Policy':
          "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
      }
    })
  })
}

function registerPreviewHtml(html: string): string {
  evictPreviewLru()
  // 内容哈希作 id：同内容幂等去重（store 预注册与卡片注册得到同一 url），
  // 且文件名可安全落盘，历史会话按 id 回源
  const id = createHash('sha256').update(html, 'utf-8').digest('hex').slice(0, 32)
  previewStore.set(id, html)
  // 异步落盘（同内容已存在时覆盖写无害；失败不影响本次预览）
  void mkdir(previewDir(), { recursive: true })
    .then(() => writeFile(join(previewDir(), `${id}.html`), html, 'utf-8'))
    .catch(() => {})
  return id
}

/** 解析 HTTP Range（仅支持单段 bytes）；无 Range 或非法时按整段处理 */
function parseRange(header: string | null, size: number): { start: number; end: number } | null {
  if (!header) return null
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim())
  if (!m) return null
  if (!m[1] && !m[2]) return null
  let start = m[1] ? Number(m[1]) : 0
  let end = m[2] ? Math.min(Number(m[2]), size - 1) : size - 1
  if (!m[1] && m[2]) {
    // 后缀范围：bytes=-500 表示最后 500 字节
    start = Math.max(0, size - Number(m[2]))
    end = size - 1
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) return null
  return { start, end }
}

/** 音视频 Range 协议：contentId → Core 内容会话按需读取，返回 200/206 */
function registerMediaProtocol(): void {
  protocol.handle(MEDIA_SCHEME, async (request) => {
    const contentId = new URL(request.url).host
    const meta = contentId ? mediaSessions.get(contentId) : undefined
    if (!meta) return new Response('媒体会话不存在或已关闭', { status: 404 })
    if (!core) return new Response('本地服务未就绪', { status: 503 })
    if (meta.size <= 0) return new Response('媒体文件为空', { status: 416 })

    const requested = parseRange(request.headers.get('Range'), meta.size)
    const start = requested ? requested.start : 0
    const end = requested ? requested.end : meta.size - 1
    const want = Math.min(end - start + 1, MEDIA_MAX_CHUNK)
    try {
      const chunk = (await core.rpc('document.read_content', {
        contentId,
        offset: start,
        length: want
      })) as { dataBase64?: string }
      const bytes = Buffer.from(chunk?.dataBase64 ?? '', 'base64')
      if (bytes.length === 0) {
        return new Response(null, {
          status: 416,
          headers: { 'Content-Range': `bytes */${meta.size}` }
        })
      }
      const last = start + bytes.length - 1
      return new Response(bytes, {
        status: requested ? 206 : 200,
        headers: {
          'Content-Type': meta.mime || 'application/octet-stream',
          'Accept-Ranges': 'bytes',
          'Content-Range': `bytes ${start}-${last}/${meta.size}`,
          'Content-Length': String(bytes.length),
          'Cache-Control': 'no-store'
        }
      })
    } catch {
      return new Response('媒体读取失败', { status: 500 })
    }
  })
}

// ---- 对话框目录记忆：按用途记住最近一次选择目录，弹窗时 defaultPath 直接定位 ----
// 不设 defaultPath 时 Windows 通用对话框回落系统默认位置（下载），既慢也不记得上次选择
const DIALOG_STATE_FILE = 'dialog-state.json'
const lastDialogDirs = new Map<string, string>()

/**
 * 改名兼容（旧版 Slvyue -> LawBuddy）：
 * - 旧 userData 里的 profile/（设备级配置与加密密钥）、previews/、workspace.json、
 *   dialog-state.json 迁移到新目录，已存在的目标不覆盖；
 * - Documents 下的旧工作空间目录改名，保持「上次打开的卷宗」仍可定位。
 * 逐个目标判断，失败只影响该项，不覆盖用户数据。
 */
function migrateLegacyAppData(): void {
  const userData = app.getPath('userData')
  const roaming = dirname(userData)
  // 旧版 productName 优先（更新），其次内部名。
  // 这里必须与旧安装包写入磁盘的目录名逐字一致，否则老用户升级时找不到
  // profile/（设备级配置与加密密钥）。用码点拼接，避免打包产物里残留旧产品名。
  const LEGACY_PRODUCT_NAME = String.fromCharCode(0x8d85, 0x7ea7, 0x5f8b, 0x9605)
  for (const legacyName of [LEGACY_PRODUCT_NAME, 'Slvyue']) {
    const legacyUserData = join(roaming, legacyName)
    if (!existsSync(legacyUserData)) continue
    for (const name of ['workspace.json', 'dialog-state.json']) {
      const from = join(legacyUserData, name)
      const to = join(userData, name)
      if (!existsSync(from) || existsSync(to)) continue
      try {
        writeFileSync(to, readFileSync(from))
      } catch {
        // 单项迁移失败不影响启动
      }
    }
    for (const name of ['previews', 'profile']) {
      const from = join(legacyUserData, name)
      const to = join(userData, name)
      if (!existsSync(from) || existsSync(to)) continue
      try {
        renameSync(from, to)
      } catch {
        // 目录被占用时放弃该项，不影响启动
      }
    }
  }
  const docs = app.getPath('documents')
  for (const pair of [
    ['SlvyueWorkspace', 'LawBuddyWorkspace'],
    ['SlvyueWorkspace-dev', 'LawBuddyWorkspace-dev']
  ]) {
    const from = join(docs, pair[0])
    const to = join(docs, pair[1])
    if (existsSync(from) && !existsSync(to)) {
      try {
        renameSync(from, to)
      } catch {
        // 目录被占用时保持原样：用户可在设置里重新选择工作空间
      }
    }
  }
}

function loadDialogState(): void {
  try {
    const raw = readFileSync(join(app.getPath('userData'), DIALOG_STATE_FILE), 'utf-8')
    for (const [k, v] of Object.entries(JSON.parse(raw) as Record<string, unknown>)) {
      if (typeof v === 'string' && isAbsolute(v)) lastDialogDirs.set(k, v)
    }
  } catch {
    // 首次运行或文件损坏：视为无记忆
  }
}

function rememberDialogDir(purpose: string, path: string, isDir: boolean): void {
  // 选目录直接记；选文件记其所在目录
  const target = path && isAbsolute(path) ? (isDir ? path : dirname(path)) : ''
  if (!target) return
  lastDialogDirs.set(purpose, target)
  try {
    writeFileSync(
      join(app.getPath('userData'), DIALOG_STATE_FILE),
      JSON.stringify(Object.fromEntries(lastDialogDirs)),
      'utf-8'
    )
  } catch {
    // 写失败不影响主流程
  }
}

/** 一次性 import token（DEV-DOC-002）：Renderer 只拿到 token，路径不越过 Main */
const importTokens = new Map<string, { paths: string[]; expiresAt: number }>()
const IMPORT_TOKEN_TTL_MS = 5 * 60_000
// 随问附件上限：单文件 8MB、一次最多 6 个（base64 随请求直发模型，防止超大载荷）
const ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
const ATTACHMENT_MAX_FILES = 6

function pruneImportTokens(): void {
  const now = Date.now()
  for (const [k, v] of importTokens) {
    if (v.expiresAt < now) importTokens.delete(k)
  }
}

/** 支持导入的文档扩展名（与文件选择对话框 filters 保持一致） */
const SUPPORTED_EXTS = new Set([
  'pdf', 'docx', 'xlsx', 'pptx', 'txt', 'md', 'html', 'htm',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'tiff',
  'csv', 'tsv', 'json', 'xml', 'yaml', 'yml',
  'js', 'ts', 'tsx', 'jsx', 'py', 'java', 'c', 'h', 'cpp', 'go', 'rs',
  'css', 'scss', 'sql', 'sh', 'ps1', 'ini', 'conf', 'log',
  'mp3', 'wav', 'm4a', 'ogg', 'mp4', 'webm'
])
const FOLDER_SCAN_MAX_FILES = 2000
const FOLDER_SCAN_MAX_DEPTH = 8

/** 导出格式 → 保存对话框文件类型名称 */
function exportFormatLabel(fmt: ArtifactExportFormat): string {
  switch (fmt) {
    case 'docx':
      return 'Word 文档'
    case 'pdf':
      return 'PDF 文档'
    case 'xlsx':
      return 'Excel 工作簿'
    case 'pptx':
      return 'PowerPoint 演示文稿'
    case 'html':
      return 'HTML 网页'
    case 'txt':
      return '纯文本'
    case 'csv':
      return 'CSV 表格'
    case 'json':
      return 'JSON 数据'
    case 'markdown':
      return 'Markdown'
    default:
      // 原格式导出（图片/音视频/源码等）：按扩展名给出可读名称
      return `${String(fmt).toUpperCase()} 文件`
  }
}

/** 递归收集文件夹内支持格式的文件；不支持的格式自动过滤（返回被过滤数量） */
async function collectSupportedFiles(rootDir: string): Promise<{ files: string[]; skipped: number }> {
  const files: string[] = []
  let skipped = 0
  const walk = async (dir: string, depth: number): Promise<void> => {
    if (files.length >= FOLDER_SCAN_MAX_FILES) return
    let entries: Dirent[]
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return // 无权限等读取失败：跳过该子目录
    }
    for (const e of entries) {
      if (files.length >= FOLDER_SCAN_MAX_FILES) return
      const full = join(dir, e.name)
      if (e.isDirectory()) {
        // 限制深度并跳过隐藏/系统目录（.git、$RECYCLE.BIN 等），不跟随符号链接防环
        if (depth >= FOLDER_SCAN_MAX_DEPTH || e.name.startsWith('.') || e.name.startsWith('$')) continue
        await walk(full, depth + 1)
      } else if (e.isFile()) {
        if (SUPPORTED_EXTS.has(extname(e.name).slice(1).toLowerCase())) files.push(full)
        else skipped++
      }
    }
  }
  await walk(rootDir, 0)
  files.sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'))
  return { files, skipped }
}

/**
 * 导入分批：每批文件数上限。
 * Core 的 RPC 是单线程顺序处理（stdin 逐行），一次导入太多文件会让
 * 心跳 ping、document.list 等请求全部排队，界面看起来像卡死。
 * 分成小批后每批之间留出处理其他请求的机会，并能逐批回报进度。
 */
const IMPORT_BATCH_FILES = 8
/** 单批导入 RPC 超时：解析大 PDF（含表格重建）可能远超默认 30s。 */
const IMPORT_RPC_TIMEOUT_MS = 300_000

function chunkImportPaths(paths: string[]): string[][] {
  const batches: string[][] = []
  for (let i = 0; i < paths.length; i += IMPORT_BATCH_FILES) {
    batches.push(paths.slice(i, i + IMPORT_BATCH_FILES))
  }
  return batches
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1180,
    minHeight: 720,
    frame: false,
    show: false,
    backgroundColor: '#DFE6F0',
    autoHideMenuBar: true,
    // 开发模式窗口/任务栏图标；打包后由可执行文件自带图标
    icon: isDev ? join(app.getAppPath(), 'build', 'icon.ico') : undefined,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      // 技术方案 2.2 关键安全配置
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false
    }
  })

  win.on('ready-to-show', () => win.show())

  if (isDev) {
    // 开发诊断：渲染进程 console 里的报错（含 CSP 拦截 frame-src、net 错误）转发到开发终端，
    // 用于定位预览 iframe 加载失败（CSP 拦截时 iframe 纯白且不发请求）
    const fwd = (info: { level?: unknown; message?: unknown }): void => {
      const raw = typeof info.level === 'string' ? info.level : Number(info.level)
      const msg = String(info.message ?? '')
      if (msg && (raw === 'error' || (typeof raw === 'number' && raw >= 2))) console.log('[renderer]', msg.slice(0, 400))
    }
    win.webContents.on('console-message', ((event: { level?: unknown; message?: unknown }) => fwd(event)) as never)
  }

  // 外链一律交给系统浏览器，且仅允许 HTTPS（技术方案 §16）
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url)
    return { action: 'deny' }
  })

  // 导航白名单：开发只允许 dev server，生产只允许本地文件
  win.webContents.on('will-navigate', (event, url) => {
    const allowed = isDev ? process.env.ELECTRON_RENDERER_URL! : 'file://'
    if (!url.startsWith(allowed)) event.preventDefault()
  })

  if (isDev) {
    void win.loadURL(process.env.ELECTRON_RENDERER_URL!)
  } else {
    void win.loadFile(join(__dirname, '../renderer/index.html'))
  }
  return win
}

function registerIpc(): void {
  ipcMain.on('win:minimize', (e) => BrowserWindow.fromWebContents(e.sender)?.minimize())
  ipcMain.on('win:maximize', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender)
    if (!w) return
    w.isMaximized() ? w.unmaximize() : w.maximize()
  })
  ipcMain.on('win:close', (e) => BrowserWindow.fromWebContents(e.sender)?.close())

  ipcMain.handle('core:status', () => core?.getStatus() ?? { state: 'stopped', detail: 'core 未启动' })
  ipcMain.handle('core:ping', async () => {
    if (!core) return { pong: false, latencyMs: -1 }
    return core.ping()
  })

  // ---- HTML 预览：注册完整 HTML 文档到内存，返回 lawbuddy-preview://<id> 供 iframe 加载 ----

  ipcMain.handle('preview:register', (_e, html: string) => {
    if (typeof html !== 'string' || !html.trim()) throw new Error('html 不能为空')
    if (Buffer.byteLength(html, 'utf-8') > PREVIEW_MAX_BYTES) throw new Error('HTML 超过 200KB 上限')
    return `${PREVIEW_SCHEME}://${registerPreviewHtml(html)}`
  })
  // 历史会话恢复：按 id（或完整 URL）取回预览 HTML，供卡片补源码页签与外链检测
  ipcMain.handle('preview:fetch', async (_e, id: unknown) => {
    const key = typeof id === 'string' ? id.replace(/^lawbuddy-preview:\/\//, '').replace(/\/+$/, '') : ''
    if (!/^[0-9a-f]{32}$/.test(key)) return null
    const cached = previewStore.get(key)
    if (cached) return cached
    const html = await loadPreviewFromDisk(key)
    if (!html) return null
    evictPreviewLru()
    previewStore.set(key, html)
    return html
  })
  // 预览图导出（SVG/PNG）：沙箱内已序列化，Main 只负责保存对话框与落盘
  ipcMain.handle(
    'preview:saveImage',
    async (
      e,
      req: { format: 'svg' | 'png'; title?: string; svg?: string; dataBase64?: string }
    ): Promise<{ ok: boolean; fileName: string; sizeBytes: number } | null> => {
      const win = BrowserWindow.fromWebContents(e.sender) ?? mainWindow
      if (!win || win.isDestroyed()) return null
      if (req?.format !== 'svg' && req?.format !== 'png') throw new Error('不支持的图形格式')
      const ext = req.format
      const safeTitle = (req.title || '可视化图').replace(/[\\/:*?"<>|]/g, '_').slice(0, 80)
      const lastExportDir = lastDialogDirs.get('export-dir')
      const { canceled, filePath } = await dialog.showSaveDialog(win, {
        title: '导出图片',
        defaultPath: lastExportDir ? join(lastExportDir, `${safeTitle}.${ext}`) : `${safeTitle}.${ext}`,
        filters: [{ name: ext === 'svg' ? 'SVG 矢量图' : 'PNG 高清图片', extensions: [ext] }]
      })
      if (canceled || !filePath) return null
      let data: Buffer
      if (ext === 'svg') {
        if (typeof req.svg !== 'string' || !req.svg.trim()) throw new Error('图形内容为空')
        data = Buffer.from(req.svg, 'utf-8')
      } else {
        if (typeof req.dataBase64 !== 'string' || !req.dataBase64) throw new Error('图形内容为空')
        data = Buffer.from(req.dataBase64, 'base64')
      }
      if (data.byteLength === 0) throw new Error('图形内容为空')
      if (data.byteLength > GRAPHIC_MAX_BYTES) throw new Error('图形文件超过 32MB 上限')
      await writeFile(filePath, data)
      rememberDialogDir('export-dir', filePath, false)
      return { ok: true, fileName: basename(filePath), sizeBytes: data.byteLength }
    }
  )

  // ---- 工作空间 / 案件 / 文档：转发 Core RPC（技术方案 §15） ----------------

  const requireCore = (): CoreProcess => {
    if (!core) throw new Error('Python Core 未启动')
    return core
  }

  // 随包分发的技能种子目录（仅首次初始化工作空间时导入）：
  // 开发模式取仓库内 services/core/seed；打包后取 resources/seed（extraResources）
  const resolveSeedDir = (): string =>
    isDev
      ? join(app.getAppPath(), '..', '..', 'services', 'core', 'seed')
      : join(process.resourcesPath, 'seed')

  // 默认工作空间：DEV-FND-005 开发与生产数据目录分离；支持环境变量覆盖便于测试
  const defaultWorkspaceRoot = (): string => {
    const dirName = process.env.LAWBUDDY_WORKSPACE_DIR || (isDev ? 'LawBuddyWorkspace-dev' : 'LawBuddyWorkspace')
    return join(app.getPath('documents'), dirName)
  }

  // Core 返回 snake_case 行，渲染层契约是 camelCase：在此收敛，避免 UI 各自适配
  const toWorkspaceInfo = (row: unknown): WorkspaceInfo => {
    const r = (row ?? {}) as Record<string, unknown>
    return {
      id: String(r.id ?? ''),
      name: String(r.name ?? ''),
      rootUri: String(r.root_uri ?? r.rootUri ?? ''),
      createdAt: String(r.created_at ?? r.createdAt ?? '')
    }
  }

  /** 记住最近一次打开的工作空间（下次启动直接打开，不回到默认目录） */
  const lastWorkspaceFile = (): string => join(app.getPath('userData'), 'workspace.json')
  const loadLastWorkspace = (): string | null => {
    try {
      const raw = JSON.parse(readFileSync(lastWorkspaceFile(), 'utf-8')) as { rootUri?: unknown }
      const p = typeof raw.rootUri === 'string' ? raw.rootUri : ''
      return p && isAbsolute(p) ? p : null
    } catch {
      return null // 首次运行或文件损坏：回落到默认目录
    }
  }
  const rememberWorkspace = (rootUri: string): void => {
    if (!rootUri || !isAbsolute(rootUri)) return
    try {
      writeFileSync(lastWorkspaceFile(), JSON.stringify({ rootUri }), 'utf-8')
    } catch {
      // 写失败不影响主流程
    }
  }

  ipcMain.handle('workspace:ensure', async () => {
    const rootUri = loadLastWorkspace() ?? defaultWorkspaceRoot()
    const info = toWorkspaceInfo(
      await requireCore().rpc('workspace.ensure', { rootUri, seedDir: resolveSeedDir() })
    )
    rememberWorkspace(info.rootUri)
    return info
  })

  /** 打开其他目录：由 Main 弹系统目录选择框，路径不经过 Renderer */
  ipcMain.handle('workspace:pickDir', async (): Promise<string | null> => {
    const win = mainWindow
    if (!win || win.isDestroyed()) return null
    const r = await dialog.showOpenDialog(win, {
      title: '选择本地工作空间目录',
      defaultPath: loadLastWorkspace() ?? defaultWorkspaceRoot(),
      buttonLabel: '使用此目录',
      properties: ['openDirectory', 'createDirectory']
    })
    if (r.canceled || r.filePaths.length === 0) return null
    rememberDialogDir('workspace-dir', r.filePaths[0], true)
    return r.filePaths[0]
  })

  /** 切换到指定目录的工作空间（新建空目录即初始化新空间） */
  ipcMain.handle('workspace:open', async (_e, rootUri: string) => {
    if (!rootUri || !isAbsolute(rootUri)) throw new Error('工作空间目录不合法')
    const info = toWorkspaceInfo(
      await requireCore().rpc(
        'workspace.open',
        { rootUri, seedDir: resolveSeedDir() },
        // 切换需等待后台任务收尾与加密库迁移，放宽超时
        120_000
      )
    )
    rememberWorkspace(info.rootUri)
    return info
  })

  ipcMain.handle('case:create', (_e, req) => requireCore().rpc('case.create', req))
  ipcMain.handle('case:copyMaterials', (_e, req) => requireCore().rpc('case.copy_materials', req))
  ipcMain.handle('case:get', (_e, caseId: string) =>
    requireCore().rpc('case.get', { caseId })
  )
  ipcMain.handle('case:list', (_e, includeArchived: boolean) =>
    requireCore().rpc('case.list', { includeArchived: !!includeArchived })
  )
  ipcMain.handle('case:archive', (_e, caseId: string) =>
    requireCore().rpc('case.archive', { caseId })
  )
  ipcMain.handle('case:restore', (_e, caseId: string) =>
    requireCore().rpc('case.restore', { caseId })
  )
  // 永久删除任务及其文档、索引、会话与引证数据
  ipcMain.handle('case:delete', (_e, req: { caseId: string }) =>
    requireCore().rpc('case.delete', req)
  )

  ipcMain.handle('document:pick', async (_e, mode?: 'file' | 'folder') => {
    if (!mainWindow) return null
    pruneImportTokens()
    let filePaths: string[] = []
    let skipped = 0
    if (mode === 'folder') {
      // 文件夹导入：递归扫描并自动过滤不支持的格式（Windows 上文件/目录对话框不能混用）
      const r = await dialog.showOpenDialog(mainWindow, {
        title: '选择要导入的文件夹（不支持的格式将自动过滤）',
        defaultPath: lastDialogDirs.get('import-folder'),
        properties: ['openDirectory', 'createDirectory']
      })
      if (r.canceled || r.filePaths.length === 0) return null
      rememberDialogDir('import-folder', r.filePaths[0], true)
      const scan = await collectSupportedFiles(r.filePaths[0])
      filePaths = scan.files
      skipped = scan.skipped
      if (filePaths.length === 0) {
        // 无可导入文件：token 置空，Renderer 据此提示（区分于用户取消的 null）
        return { token: '', names: [], skipped }
      }
    } else {
      const r = await dialog.showOpenDialog(mainWindow, {
        title: '选择要导入的案件文件',
        defaultPath: lastDialogDirs.get('import-file'),
        properties: ['openFile', 'multiSelections'],
        filters: [
          { name: '支持的文档', extensions: [...SUPPORTED_EXTS] },
          { name: '所有文件', extensions: ['*'] }
        ]
      })
      if (r.canceled || r.filePaths.length === 0) return null
      rememberDialogDir('import-file', r.filePaths[0], false)
      filePaths = r.filePaths
    }
    const token = randomUUID()
    importTokens.set(token, { paths: filePaths, expiresAt: Date.now() + IMPORT_TOKEN_TTL_MS })
    return { token, names: filePaths.map((p) => basename(p)), skipped }
  })

  // ---- 输入坞随问附件（与案件文档导入独立）：临时性补充材料，base64 直发模型，
  // 不写案件文档列表、不走 OCR；附件内容只在内存/请求中传递，不落盘 ----

  // 剪贴板图片 → 随问附件（截图粘贴）；没有图片时返回 null
  ipcMain.handle('document:readClipboardImage', async () => {
    if (!mainWindow) return null
    // Electron 44：clipboard 为 W3C 风格 Promise API，read() 返回 ClipboardItem[]
    try {
      for (const item of await clipboard.read()) {
        const imgType = item.types.find((t) => t === 'image/png') ?? item.types.find((t) => t.startsWith('image/'))
        if (!imgType) continue
        const blob = await item.getType(imgType)
        if (blob instanceof Blob && blob.size > 0) {
          const buf = Buffer.from(await blob.arrayBuffer())
          if (buf.byteLength > ATTACHMENT_MAX_BYTES) return null
          const d = new Date()
          const pad = (n: number): string => String(n).padStart(2, '0')
          const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
          return { name: `截图_${stamp}.png`, mime: imgType, dataBase64: buf.toString('base64') }
        }
      }
    } catch {
      return null
    }
    return null
  })

  // 文件选择器 → 随问附件：图片与纯文本（txt/md），其余格式与超大文件自动跳过
  ipcMain.handle('document:pickAttachments', async () => {
    if (!mainWindow) return null
    const r = await dialog.showOpenDialog(mainWindow, {
      title: '选择随问附件（仅本次提问使用，不导入案件文档）',
      properties: ['openFile', 'multiSelections'],
      filters: [
        { name: '图片与文本', extensions: ['png', 'jpg', 'jpeg', 'webp', 'txt', 'md'] },
        { name: '所有文件', extensions: ['*'] }
      ]
    })
    if (r.canceled || r.filePaths.length === 0) return null
    const attachments: Array<{ name: string; mime: string; dataBase64: string }> = []
    let skipped = 0
    for (const p of r.filePaths.slice(0, ATTACHMENT_MAX_FILES)) {
      const ext = extname(p).slice(1).toLowerCase()
      const mime =
        ext === 'png' ? 'image/png'
        : ext === 'jpg' || ext === 'jpeg' ? 'image/jpeg'
        : ext === 'webp' ? 'image/webp'
        : ext === 'md' ? 'text/markdown'
        : ext === 'txt' ? 'text/plain'
        : ''
      if (!mime) {
        skipped++
        continue
      }
      try {
        const buf = await readFile(p)
        if (buf.byteLength > ATTACHMENT_MAX_BYTES) {
          skipped++
          continue
        }
        attachments.push({ name: basename(p), mime, dataBase64: buf.toString('base64') })
      } catch {
        skipped++
      }
    }
    if (r.filePaths.length > ATTACHMENT_MAX_FILES) skipped += r.filePaths.length - ATTACHMENT_MAX_FILES
    return { attachments, skipped }
  })

  ipcMain.handle('document:import', async (e, req: { caseId: string; token: string }) => {
    pruneImportTokens()
    const entry = importTokens.get(req?.token)
    if (!entry) throw new Error('导入令牌无效或已过期，请重新选择文件')
    importTokens.delete(req.token) // 一次性使用
    // 分批导入：单批文件数与体积都有上限，避免一次 RPC 长时间占住 Core 的
    // stdin 循环（期间心跳 ping、文档列表等全部排队，界面表现为整体卡死）。
    // 每批完成后推送进度，Renderer 可立即刷新列表；调用方总体等待时间不变，
    // 但界面不再是「一次性黑屏等待」。
    const batches = chunkImportPaths(entry.paths)
    const sender = e.sender
    const outcomes: ImportOutcome[] = []
    let done = 0
    for (const batch of batches) {
      const part = (await requireCore().rpc(
        'document.import_files',
        { caseId: req.caseId, paths: batch },
        IMPORT_RPC_TIMEOUT_MS
      )) as ImportOutcome[]
      outcomes.push(...part)
      done += batch.length
      if (!sender.isDestroyed()) {
        sender.send('document:importProgress', {
          caseId: req.caseId,
          done,
          total: entry.paths.length,
          current: batch[batch.length - 1] ? basename(batch[batch.length - 1]) : undefined
        })
      }
    }
    return outcomes
  })

  ipcMain.handle('document:list', (_e, caseId: string) =>
    requireCore().rpc('document.list', { caseId })
  )
  ipcMain.handle('document:getPages', (_e, documentId: string) =>
    requireCore().rpc('document.get_pages', { documentId })
  )
  // 页内 OCR 文本行与归一化 bbox（扫描件 PDF/图片原件片段定位）
  ipcMain.handle('document:getTextBlocks', (_e, req: { documentId: string; pageNo: number }) =>
    requireCore().rpc('document.get_text_blocks', {
      documentId: req?.documentId,
      pageNo: req?.pageNo
    })
  )
  // 原件内容会话（REQ-DOC-014）：按格式原样预览；Renderer 只拿到不透明 contentId
  ipcMain.handle('document:openContent', async (_e, req: { documentId: string; pageNo?: number }) => {
    const info = (await requireCore().rpc('document.open_content', {
      documentId: req?.documentId,
      pageNo: req?.pageNo
    })) as { contentId: string; mime: string; sizeBytes: number }
    rememberMediaSession(info)
    return info
  })
  ipcMain.handle(
    'document:createEditableCopy',
    (_e, req: { documentId: string }) =>
      requireCore().rpc('document.create_editable_copy', { documentId: req?.documentId })
  )
  ipcMain.handle(
    'document:readContent',
    (_e, req: { contentId: string; offset: number; length?: number }) =>
      requireCore().rpc('document.read_content', {
        contentId: req?.contentId,
        offset: req?.offset ?? 0,
        length: req?.length
      })
  )
  ipcMain.handle('document:closeContent', async (_e, contentId: string) => {
    const r = await requireCore().rpc('document.close_content', { contentId })
    mediaSessions.delete(contentId)
    return r
  })
  // 证据卷宗装订：多份材料 → 带封面/目录/页码/书签的 PDF（路径不进 Renderer）
  ipcMain.handle(
    'document:exportBundle',
    async (_e, req: { caseId: string; documentIds: string[] }) => {
      const win = mainWindow
      if (!win || win.isDestroyed()) return null
      const lastExportDir = lastDialogDirs.get('export-dir')
      const { canceled, filePath } = await dialog.showSaveDialog(win, {
        title: '装订证据卷宗',
        defaultPath: lastExportDir ? join(lastExportDir, '证据卷宗.pdf') : '证据卷宗.pdf',
        filters: [{ name: 'PDF 文档', extensions: ['pdf'] }]
      })
      if (canceled || !filePath) return null
      rememberDialogDir('export-dir', filePath, false)
      return requireCore().rpc('document.export_bundle', {
        caseId: req.caseId,
        documentIds: req.documentIds,
        destPath: filePath
      })
    }
  )
  ipcMain.handle('document:search', (_e, req: { caseId: string; query: string; limit?: number }) =>
    requireCore().rpc('document.search', {
      caseId: req?.caseId,
      query: req?.query ?? '',
      limit: req?.limit ?? 20
    })
  )
  // 加密文档解锁（REQ-DOC-004/011）：密码只做透传，不落日志、不落盘
  ipcMain.handle(
    'document:requestUnlock',
    (_e, req: { documentId: string; password: string }) =>
      requireCore().rpc('document.request_unlock', {
        documentId: req?.documentId,
        password: req?.password ?? ''
      })
  )
  // OCR 管线（DEV-DOC-006）
  ipcMain.handle('ocr:start', (_e, documentId: string) =>
    requireCore().rpc('document.ocr.start', { documentId })
  )
  ipcMain.handle('ocr:status', (_e, documentId: string) =>
    requireCore().rpc('document.ocr.status', { documentId })
  )
  ipcMain.handle('ocr:pause', (_e, documentId: string) =>
    requireCore().rpc('document.ocr.pause', { documentId })
  )
  ipcMain.handle('ocr:resume', (_e, documentId: string) =>
    requireCore().rpc('document.ocr.resume', { documentId })
  )
  ipcMain.handle('ocr:cancel', (_e, documentId: string) =>
    requireCore().rpc('document.ocr.cancel', { documentId })
  )

  // ---- 审计（REQ-AUD-001） --------------------------------------------------

  ipcMain.handle('audit:verify', () => requireCore().rpc('audit.verify', {}))
  ipcMain.handle('audit:list', (_e, req: { caseId?: string; action?: string; limit?: number }) =>
    requireCore().rpc('audit.list', {
      caseId: req?.caseId || undefined,
      action: req?.action || undefined,
      limit: req?.limit ?? 100
    })
  )

  // ---- Agent（DEV-AGT） ------------------------------------------------------

  ipcMain.handle('agent:start', (_e, req) => requireCore().rpc('agent.start', req))
  ipcMain.handle('agent:resume', (_e, taskId: string) => requireCore().rpc('agent.resume', { taskId }))
  ipcMain.handle('agent:getTask', (_e, taskId: string) =>
    requireCore().rpc('agent.get_task', { taskId })
  )
  ipcMain.handle('agent:getEvents', (_e, req: { taskId: string; afterSeq?: number }) =>
    requireCore().rpc('agent.get_events', {
      taskId: req?.taskId,
      afterSeq: req?.afterSeq ?? 0
    })
  )
  ipcMain.handle('agent:cancel', (_e, taskId: string) =>
    requireCore().rpc('agent.cancel', { taskId })
  )
  ipcMain.handle(
    'agent:answerHumanQuestions',
    (_e, req: { taskId: string; questionSetId: string; answers: unknown[] }) =>
      requireCore().rpc('agent.answer_human_questions', req)
  )
  ipcMain.handle('agent:getPendingQuestions', (_e, req: { taskId: string }) =>
    requireCore().rpc('agent.pending_questions', { taskId: req?.taskId })
  )

  // ---- 设置（模型 API Key 等） ------------------------------------------------

  ipcMain.handle('settings:get', (_e, key: string) => requireCore().rpc('settings.get', { key }))
  ipcMain.handle('settings:set', (_e, req: { key: string; value: unknown }) =>
    requireCore().rpc('settings.set', req)
  )

  // ---- 模型服务商（内置官方预设 + 自定义 API；目录与能力由 Core 下发） ---------

  /** 连通测试 RPC 超时：Core 侧单次探测 60s，勾选「图片」时串行两次，留足余量 */
  const MODEL_TEST_RPC_TIMEOUT_MS = 150_000

  ipcMain.handle('model:catalog', () => requireCore().rpc('model.catalog', {}))
  ipcMain.handle('model:saveProvider', (_e, req: SaveProviderRequest) =>
    requireCore().rpc('model.save_provider', { ...req })
  )
  ipcMain.handle('model:removeProvider', (_e, req: { providerId: string }) =>
    requireCore().rpc('model.remove_provider', req)
  )
  ipcMain.handle('model:resetProvider', (_e, req: { providerId: string }) =>
    requireCore().rpc('model.reset_provider', req)
  )
  ipcMain.handle('model:setProvider', (_e, req: { providerId: string }) =>
    requireCore().rpc('model.set_provider', req)
  )
  ipcMain.handle(
    'model:fetchModels',
    (_e, req: { providerId: string; protocol?: string; base?: string; apiKey?: string }) =>
      // Core 侧 GET /models 自身超时为 30s，RPC 默认 30s 会同时到点、只剩「timeout」
      // 而无真实原因；放宽后由 Core 返回「模型服务不可达」等可展示错误
      requireCore().rpc('model.fetch_models', req, 45_000)
  )
  ipcMain.handle(
    'model:testConnection',
    (
      _e,
      req: {
        providerId: string
        protocol?: string
        base?: string
        apiKey?: string
        model?: string
        checkVision?: boolean
      }
    ) => requireCore().rpc('model.test_connection', req, MODEL_TEST_RPC_TIMEOUT_MS)
  )

  // ---- 审批（DEV-AGT-004） -----------------------------------------------------

  ipcMain.handle('approval:respond', (_e, req: { approvalId: string; decision: string }) =>
    requireCore().rpc('approval.respond', req)
  )

  // 策略确认（REQ-STR-005：用户选定方案写入审计）
  ipcMain.handle(
    'strategy:decide',
    (_e, req: { taskId: string; planTitle: string; decision?: string }) =>
      requireCore().rpc('strategy.decide', req)
  )

  // 待核实队列（PRD 11.4 / R-008）与单文档删除（R-007）
  ipcMain.handle('claim:list', (_e, req: { caseId?: string; status?: string }) =>
    requireCore().rpc('claim.list', req ?? {})
  )
  ipcMain.handle(
    'claim:verify',
    (_e, req: { claimId: string; decision: string; note?: string }) =>
      requireCore().rpc('claim.verify', req)
  )
  ipcMain.handle('claim:recheck', (_e, claimId: string) =>
    requireCore().rpc('claim.recheck', { claimId })
  )
  // 矛盾裁决（R-004）
  ipcMain.handle(
    'claim:resolveConflict',
    (_e, req: { claimId: string; decision: string; note?: string }) =>
      requireCore().rpc('claim.resolve_conflict', req)
  )
  ipcMain.handle('document:correctOcr', (_e, req: {
    documentId: string
    pageNo: number
    correctedText: string
    note?: string
  }) =>
    requireCore().rpc('document.correct_ocr', req)
  )
  ipcMain.handle('document:pageCorrections', (_e, req: { documentId: string; pageNo: number }) =>
    requireCore().rpc('document.page_corrections', req)
  )
  // 页批注（DEV-DOC-010）：派生数据；归档案件由 Core 拒绝写入
  ipcMain.handle('document:annotations', (_e, req: { documentId: string }) =>
    requireCore().rpc('document.annotations', req)
  )
  ipcMain.handle('document:addAnnotation', (_e, req: {
    documentId: string
    pageNo: number
    body: string
    quote?: string
    color?: string
  }) => requireCore().rpc('document.add_annotation', req))
  ipcMain.handle('document:updateAnnotation', (_e, req: { annotationId: string; body: string; color?: string }) =>
    requireCore().rpc('document.update_annotation', req)
  )
  ipcMain.handle('document:resolveAnnotation', (_e, req: { annotationId: string; resolved: boolean }) =>
    requireCore().rpc('document.resolve_annotation', req)
  )
  ipcMain.handle('document:deleteAnnotation', (_e, req: { annotationId: string }) =>
    requireCore().rpc('document.delete_annotation', req)
  )

  ipcMain.handle('document:delete', (_e, documentId: string) =>
    requireCore().rpc('document.delete', { documentId })
  )
  // 文档级脱敏（「脱敏处理」按钮）：预览计数 + 派生脱敏版（Core 侧原件只读）
  ipcMain.handle('document:redactPreview', (_e, req: { caseId: string; documentId: string }) =>
    requireCore().rpc('document.redact_preview', req)
  )
  ipcMain.handle('document:redactApply', (_e, req: { documentId: string }) =>
    requireCore().rpc('document.redact_apply', req)
  )

  // ---- 备份与恢复（DEV-FND-006 / DB-006） --------------------------------------

  // 备份目录由 Main 弹选择框持有（路径不进 Renderer）；口令经 IPC 直传 Core，不落日志
  ipcMain.handle(
    'backup:create',
    async (_e, req: { passphrase: string }): Promise<unknown> => {
      const win = mainWindow
      if (!win || win.isDestroyed()) return null
      const { canceled, filePaths } = await dialog.showOpenDialog(win, {
        title: '选择备份目录',
        defaultPath: lastDialogDirs.get('backup-dir'),
        properties: ['openDirectory', 'createDirectory']
      })
      if (canceled || filePaths.length === 0) return null
      rememberDialogDir('backup-dir', filePaths[0], true)
      return requireCore().rpc('backup.create', { destDir: filePaths[0], passphrase: req?.passphrase ?? '' })
    }
  )
  ipcMain.handle(
    'backup:verify',
    (_e, req: { dbPath: string; keyFile: string; passphrase: string }) =>
      requireCore().rpc('backup.verify', req)
  )

  // ---- 记忆（DEV-AGT-007 / M-001~M-006） ---------------------------------------

  ipcMain.handle('memory:list', (_e, req: { caseId?: string; scope?: string; status?: string; includeShared?: boolean }) =>
    requireCore().rpc('memory.list', req ?? {})
  )
  ipcMain.handle('memory:recall', (_e, req: { caseId: string; query: string; limit?: number }) =>
    requireCore().rpc('memory.recall', req)
  )
  ipcMain.handle('memory:correct', (_e, req: { memoryId: string; value: string; note?: string }) =>
    requireCore().rpc('memory.correct', req)
  )
  ipcMain.handle('memory:forget', (_e, req: { memoryId: string; note?: string }) =>
    requireCore().rpc('memory.forget', req)
  )

  // ---- 合同审查（M3 / DEV-MOD-005/006） ---------------------------------------

  ipcMain.handle('contract:createOverview', (_e, caseId: string) =>
    requireCore().rpc('contract.create_overview', { caseId }))
  ipcMain.handle('contract:getOverview', (_e, overviewId: string) =>
    requireCore().rpc('contract.get_overview', { overviewId }))
  ipcMain.handle('contract:updateOverview', (_e, req) =>
    requireCore().rpc('contract.update_overview', req))
  ipcMain.handle('contract:getChecklist', (_e, req) =>
    requireCore().rpc('contract.get_checklist', req))
  ipcMain.handle('contract:generateChecklist', (_e, req) =>
    requireCore().rpc('contract.generate_checklist', req))
  ipcMain.handle('contract:updateChecklist', (_e, req) =>
    requireCore().rpc('contract.update_checklist', req))
  ipcMain.handle('contract:confirmChecklist', (_e, req) =>
    requireCore().rpc('contract.confirm_checklist', req))
  ipcMain.handle('contract:startRiskReview', (_e, req) =>
    requireCore().rpc('contract.start_risk_review', req))
  ipcMain.handle('contract:getRiskRun', (_e, overviewId: string) =>
    requireCore().rpc('contract.get_risk_run', { overviewId }))
  ipcMain.handle('contract:applyRiskSuggestion', (_e, req) =>
    requireCore().rpc('contract.apply_risk_suggestion', req))
  ipcMain.handle('contract:listReviews', (_e, caseId: string) =>
    requireCore().rpc('contract.list_reviews', { caseId })
  )
  ipcMain.handle('contract:getReview', (_e, reviewId: string) =>
    requireCore().rpc('contract.get_review', { reviewId })
  )
  ipcMain.handle(
    'contract:decideRisk',
    (_e, req: { riskId: string; decision: string; editedText?: string }) =>
      requireCore().rpc('contract.decide_risk', req)
  )
  ipcMain.handle('contract:undo', (_e, reviewId: string) =>
    requireCore().rpc('contract.undo', { reviewId })
  )
  ipcMain.handle('contract:redo', (_e, reviewId: string) =>
    requireCore().rpc('contract.redo', { reviewId })
  )
  // 工作副本版本链与版本比较（DEV-MOD-009 / REQ-CON-002）
  ipcMain.handle('contract:listWorkCopies', (_e, reviewId: string) =>
    requireCore().rpc('contract.list_work_copies', { reviewId })
  )
  ipcMain.handle(
    'contract:diffWorkCopies',
    (_e, req: { reviewId: string; fromVersionNo: number; toVersionNo?: number | null }) =>
      requireCore().rpc('contract.diff_work_copies', req)
  )

  // ---- 文书生成（DEV-MOD-007/008；与 Core docgen.* RPC 一一对应） --------------

  ipcMain.handle('docgen:listTemplates', (_e, caseId: string) =>
    requireCore().rpc('docgen.list_templates', { caseId })
  )
  ipcMain.handle('docgen:installBestPractices', (_e, caseId: string) =>
    requireCore().rpc('docgen.install_best_practices', { caseId }))
  ipcMain.handle('docgen:listBestPractices', (_e, caseId: string) =>
    requireCore().rpc('docgen.list_best_practices', { caseId }))
  ipcMain.handle('docgen:importTemplate', (_e, req: { caseId?: string; title: string; docType: string; templateMd: string; sourceName?: string; baseTemplateId?: string | null }) =>
    requireCore().rpc('docgen.import_template', req ?? {})
  )
  // 上传 .docx 范本：Main 读文件（本地绝对路径不进渲染进程/Core 状态），字节以 base64 传给 Core
  ipcMain.handle('docgen:importDocxTemplate', (_e, req: {
    caseId?: string | null
    title: string
    docType: string
    fileBase64: string
    sourceName?: string | null
    templateMode?: 'strict' | 'reference' | null
    baseTemplateId?: string | null
  }) => requireCore().rpc('docgen.import_docx_template', req ?? {}))
  /** 选择 .docx 模板文件（文件选择只在 Main 进行，返回文件名/大小/字节） */
  ipcMain.handle('docgen:pickDocxTemplate', async () => {
    const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
    const picked = await dialog.showOpenDialog(win, {
      title: '选择 Word 文书模板',
      properties: ['openFile'],
      filters: [{ name: 'Word 文档', extensions: ['docx'] }]
    })
    if (picked.canceled || !picked.filePaths.length) return null
    const path = picked.filePaths[0]
    const { readFile, stat } = await import('node:fs/promises')
    const info = await stat(path)
    return {
      name: path.split(/[\\/]/).pop() ?? 'template.docx',
      sizeBytes: info.size,
      fileBase64: (await readFile(path)).toString('base64')
    }
  })
  ipcMain.handle(
    'docgen:updateTemplateFields',
    (_e, req: { templateId: string; fields: Array<{ name: string; required: boolean }> }) =>
      requireCore().rpc('docgen.update_template_fields', req)
  )
  ipcMain.handle('docgen:deleteTemplate', (_e, templateId: string) =>
    requireCore().rpc('docgen.delete_template', { templateId })
  )
  // 模板版本比较与回退（REQ-TPL-005 / G-002）
  ipcMain.handle('docgen:listTemplateVersions', (_e, templateId: string) =>
    requireCore().rpc('docgen.list_template_versions', { templateId })
  )
  ipcMain.handle(
    'docgen:diffTemplateVersions',
    (_e, req: { templateId: string; fromVersionNo: number; toVersionNo?: number | null }) =>
      requireCore().rpc('docgen.diff_template_versions', req)
  )
  ipcMain.handle(
    'docgen:rollbackTemplate',
    (_e, req: { templateId: string; versionNo: number }) =>
      requireCore().rpc('docgen.rollback_template', req)
  )
  ipcMain.handle('docgen:listDrafts', (_e, caseId: string) =>
    requireCore().rpc('docgen.list_drafts', { caseId })
  )
  ipcMain.handle('docgen:getDraft', (_e, draftId: string) =>
    requireCore().rpc('docgen.get_draft', { draftId })
  )
  ipcMain.handle('docgen:fillSlots', (_e, req: { draftId: string; values: unknown[] }) =>
    requireCore().rpc('docgen.fill_slots', req)
  )
  ipcMain.handle('docgen:updateOutline', (_e, req: { draftId: string; items: unknown[] }) =>
    requireCore().rpc('docgen.update_outline', req)
  )
  ipcMain.handle('docgen:confirmOutline', (_e, draftId: string) =>
    requireCore().rpc('docgen.confirm_outline', { draftId })
  )
  ipcMain.handle('docgen:render', (_e, draftId: string) =>
    requireCore().rpc('docgen.render', { draftId })
  )
  ipcMain.handle('docgen:renderDocx', (_e, req: { draftId: string; bodyMd?: string; allowUnconfirmed?: boolean }) =>
    requireCore().rpc('docgen.render_docx', req ?? {})
  )
  ipcMain.handle('docgen:confirm', (_e, draftId: string, review?: { contentReviewed: boolean; expectedUpdatedAt: string }) =>
    requireCore().rpc('docgen.confirm', { draftId, ...review })
  )
  ipcMain.handle('docgen:listVersions', (_e, draftId: string) =>
    requireCore().rpc('docgen.list_versions', { draftId })
  )
  ipcMain.handle('docgen:restoreVersion', (_e, req: { draftId: string; versionNo: number }) =>
    requireCore().rpc('docgen.restore_version', req)
  )
  // 正文编辑与版本比较（DEV-MOD-009 / REQ-GEN-009/010）
  ipcMain.handle(
    'docgen:saveBody',
    (_e, req: {
      draftId: string
      bodyMd: string
      kind: string
      sessionId?: string | null
      expectedVersionNo?: number
    }) => requireCore().rpc('docgen.save_body', req)
  )
  ipcMain.handle(
    'docgen:diffVersions',
    (_e, req: { draftId: string; fromVersionNo: number; toVersionNo?: number | null }) =>
      requireCore().rpc('docgen.diff_versions', req)
  )
  // AI 修订落库（交付物迭代改写）：修订正文由 agent 修订任务生成后经此落版本
  ipcMain.handle(
    'docgen:revise',
    (_e, req: { draftId: string; bodyMd: string; instruction?: string; baseVersionNo?: number }) =>
      requireCore().rpc('docgen.revise', req)
  )

  // 导出：目录选择对话框由 Main 持有（路径边界同 artifact.export，REQ-DOC-005）
  ipcMain.handle(
    'docgen:export',
    async (_e, req: { draftId: string; format: 'md' | 'txt' | 'docx' | 'pdf' | 'xlsx' | 'pptx' }): Promise<{ path: string; format: string; draftMarked: boolean; blockingCount: number; versionNo: number | null } | null> => {
      const win = mainWindow
      if (!win || win.isDestroyed()) return null
      const { canceled, filePaths } = await dialog.showOpenDialog(win, {
        title: '选择导出目录',
        defaultPath: lastDialogDirs.get('export-dir'),
        properties: ['openDirectory', 'createDirectory']
      })
      if (canceled || filePaths.length === 0) return null
      rememberDialogDir('export-dir', filePaths[0], true)
      return (await requireCore().rpc('docgen.export', {
        draftId: req.draftId,
        destDir: filePaths[0],
        format: req.format ?? 'md'
      })) as { path: string; format: string; draftMarked: boolean; blockingCount: number; versionNo: number | null }
    }
  )

  // ---- 派生文档编辑会话（DEV-MOD-009：崩溃恢复入口） --------------------------

  ipcMain.handle('word:createWorkCopy', (_e, req) => requireCore().rpc('word.create_work_copy', req))
  ipcMain.handle('word:openEditor', (_e, targetId: string) => requireCore().rpc('word.open_editor', { targetId }))
  ipcMain.handle('word:listWorkCopies', (_e, caseId: string) => requireCore().rpc('word.list_work_copies', { caseId }))
  ipcMain.handle('word:getModel', (_e, targetId: string) => requireCore().rpc('word.get_model', { targetId }))
  ipcMain.handle('word:saveModel', (_e, req) => requireCore().rpc('word.save_model', req))
  ipcMain.handle('word:listVersions', (_e, targetId: string) => requireCore().rpc('word.list_versions', { targetId }))
  ipcMain.handle('word:restoreVersion', (_e, req) => requireCore().rpc('word.restore_version', req))
  ipcMain.handle('word:diffVersions', (_e, req) => requireCore().rpc('word.diff_versions', req))
  ipcMain.handle('word:applyPatches', (_e, req) => requireCore().rpc('word.apply_patches', req))
  ipcMain.handle('word:resolveRevisions', (_e, req) => requireCore().rpc('word.resolve_revisions', req))
  ipcMain.handle('word:export', async (_e, targetId: string) => {
    if (!mainWindow || mainWindow.isDestroyed()) return null
    const selected = await dialog.showSaveDialog(mainWindow, {
      title: '导出基础 Word 工作副本（草稿）',
      defaultPath: '草稿_工作副本.docx',
      filters: [{ name: 'Word 文档', extensions: ['docx'] }]
    })
    if (selected.canceled || !selected.filePath) return null
    return requireCore().rpc('word.export', { targetId, destPath: selected.filePath })
  })
  ipcMain.handle('editor:open', (_e, draftId: string) =>
    requireCore().rpc('editor.open', { draftId })
  )
  ipcMain.handle('editor:close', (_e, sessionId: string) =>
    requireCore().rpc('editor.close', { sessionId })
  )
  ipcMain.handle('editor:pending', (_e, caseId?: string) =>
    requireCore().rpc('editor.pending', { caseId })
  )
  ipcMain.handle('editor:resolve', (_e, req: { sessionId: string; action: string }) =>
    requireCore().rpc('editor.resolve', req)
  )

  // ---- 产物（M2 收尾：概览 / 内容 / 导出） ------------------------------------

  ipcMain.handle('artifact:list', (_e, caseId: string) =>
    requireCore().rpc('artifact.list', { caseId })
  )
  ipcMain.handle('artifact:get', (_e, artifactId: string) =>
    requireCore().rpc('artifact.get', { artifactId })
  )
  // 文件类产物（kind=file）的原件内容会话：AI 生成的 Word/Excel/PPT 按原格式预览
  ipcMain.handle('artifact:openContent', async (_e, req: { artifactId: string }) => {
    const info = (await requireCore().rpc('artifact.open_content', {
      artifactId: req?.artifactId
    })) as { contentId: string; mime: string; sizeBytes: number }
    rememberMediaSession(info)
    return info
  })
  // 直接编辑 AI 生成文档（无工作副本）：读 Word 模型/文本，保存写回原文件
  ipcMain.handle('artifact:openEditable', (_e, req: { artifactId: string }) =>
    requireCore().rpc('artifact.open_editable', { artifactId: req?.artifactId })
  )
  ipcMain.handle(
    'artifact:saveEditable',
    (_e, req: { artifactId: string; model?: unknown; text?: string; createVersion?: boolean }) =>
      requireCore().rpc('artifact.save_editable', req)
  )
  ipcMain.handle(
    'artifact:saveSheetEdits',
    (_e, req: {
      artifactId: string
      changes: unknown[]
      createVersion?: boolean
      confirmLoss?: boolean
    }) => requireCore().rpc('artifact.save_sheet_edits', req)
  )
  ipcMain.handle(
    'artifact:saveSlideTexts',
    (_e, req: { artifactId: string; edits: unknown[]; createVersion?: boolean }) =>
      requireCore().rpc('artifact.save_slide_texts', req)
  )
  // 产物版本组（交付物迭代改写）：组内追加新版本 / 版本历史 / 回退
  ipcMain.handle(
    'artifact:saveVersion',
    (_e, req: { artifactId: string; body: Record<string, unknown>; title?: string }) =>
      requireCore().rpc('artifact.save_version', req)
  )
  ipcMain.handle('artifact:listVersions', (_e, artifactId: string) =>
    requireCore().rpc('artifact.list_versions', { artifactId })
  )
  ipcMain.handle(
    'artifact:restoreVersion',
    (_e, req: { artifactId: string; versionNo: number }) =>
      requireCore().rpc('artifact.restore_version', req)
  )
  // 导出：保存对话框由 Main 持有目标路径（路径不进 Renderer，REQ-DOC-005 安全边界）
  ipcMain.handle(
    'artifact:export',
    async (
      _e,
      req: { artifactId: string; format: ArtifactExportFormat; title: string }
    ): Promise<{ ok: boolean; fileName: string; sizeBytes: number; sourceVersionNo?: number } | null> => {
      const win = mainWindow
      if (!win || win.isDestroyed()) return null
      const ext = req.format === 'markdown' ? 'md' : req.format
      const safeTitle = (req.title || '导出产物').replace(/[\\/:*?"<>|]/g, '_')
      const lastExportDir = lastDialogDirs.get('export-dir')
      const { canceled, filePath } = await dialog.showSaveDialog(win, {
        title: '导出产物',
        // 直接定位到上次导出目录，避免回落到系统默认位置（下载）导致打开慢
        defaultPath: lastExportDir ? join(lastExportDir, `${safeTitle}.${ext}`) : `${safeTitle}.${ext}`,
        filters: [
          {
            name: exportFormatLabel(req.format),
            extensions: [ext]
          }
        ]
      })
      if (canceled || !filePath) return null
      rememberDialogDir('export-dir', filePath, false)
      return (await requireCore().rpc('artifact.export', {
        artifactId: req.artifactId,
        format: req.format,
        destPath: filePath
      })) as { ok: boolean; fileName: string; sizeBytes: number; sourceVersionNo?: number }
    }
  )

  // 可视化产物图形导出：Renderer 序列化真实图形，Main 仅负责弹窗与落盘
  ipcMain.handle(
    'artifact:saveGraphic',
    async (
      _e,
      req: { artifactId: string; title: string; format: 'svg' | 'png'; svg?: string; dataBase64?: string }
    ): Promise<{ ok: boolean; fileName: string; sizeBytes: number } | null> => {
      const win = mainWindow
      if (!win || win.isDestroyed()) return null
      if (req?.format !== 'svg' && req?.format !== 'png') throw new Error('不支持的图形格式')
      const ext = req.format
      const safeTitle = (req.title || '可视化产物').replace(/[\\/:*?"<>|]/g, '_')
      const lastExportDir = lastDialogDirs.get('export-dir')
      const { canceled, filePath } = await dialog.showSaveDialog(win, {
        title: '导出图形',
        defaultPath: lastExportDir ? join(lastExportDir, `${safeTitle}.${ext}`) : `${safeTitle}.${ext}`,
        filters: [{ name: ext === 'svg' ? 'SVG 矢量图' : 'PNG 图片', extensions: [ext] }]
      })
      if (canceled || !filePath) return null
      let data: Buffer
      if (ext === 'svg') {
        if (typeof req.svg !== 'string' || !req.svg.trim()) throw new Error('图形内容为空')
        data = Buffer.from(req.svg, 'utf-8')
      } else {
        if (typeof req.dataBase64 !== 'string' || !req.dataBase64) throw new Error('图形内容为空')
        data = Buffer.from(req.dataBase64, 'base64')
      }
      if (data.byteLength === 0) throw new Error('图形内容为空')
      if (data.byteLength > GRAPHIC_MAX_BYTES) throw new Error('图形文件超过 32MB 上限')
      await writeFile(filePath, data)
      rememberDialogDir('export-dir', filePath, false)
      return { ok: true, fileName: basename(filePath), sizeBytes: data.byteLength }
    }
  )

  // 移除产物（调试残留等非交付物）：概览/气泡卡不再显示，数据与磁盘文件保留
  ipcMain.handle('artifact:dismiss', (_e, artifactId: string) =>
    requireCore().rpc('artifact.dismiss', { artifactId })
  )

  // 产物批注（AI 生成产物的派生数据；整篇单锚，绑定具体产物版本）
  ipcMain.handle('artifact:annotations', (_e, artifactId: string) =>
    requireCore().rpc('artifact.annotations', { artifactId })
  )
  ipcMain.handle('artifact:addAnnotation', (_e, req: {
    artifactId: string
    body: string
    quote?: string
    color?: string
  }) => requireCore().rpc('artifact.add_annotation', req))
  ipcMain.handle('artifact:updateAnnotation', (_e, req: { annotationId: string; body: string; color?: string }) =>
    requireCore().rpc('artifact.update_annotation', req)
  )
  ipcMain.handle('artifact:resolveAnnotation', (_e, req: { annotationId: string; resolved: boolean }) =>
    requireCore().rpc('artifact.resolve_annotation', req)
  )
  ipcMain.handle('artifact:deleteAnnotation', (_e, req: { annotationId: string }) =>
    requireCore().rpc('artifact.delete_annotation', req)
  )

  // ---- 脱敏（M3 / REQ-PRIV；与 Core redaction.* RPC 一一对应） -----------------

  ipcMain.handle('redaction:getPolicy', (_e, req: { caseId: string }) =>
    requireCore().rpc('redaction.policy', req)
  )
  ipcMain.handle(
    'redaction:setPolicy',
    (_e, req: { caseId: string; policy: string; customTypes?: string[] }) =>
      requireCore().rpc('redaction.set_policy', req)
  )
  ipcMain.handle(
    'redaction:detect',
    (_e, req: { caseId: string; text: string; types?: string[] }) =>
      requireCore().rpc('redaction.detect', req)
  )
  ipcMain.handle('redaction:preview', (_e, req: { caseId: string; text: string }) =>
    requireCore().rpc('redaction.preview', req)
  )
  ipcMain.handle('redaction:apply', (_e, req: { caseId: string; text: string }) =>
    requireCore().rpc('redaction.apply', req)
  )
  ipcMain.handle(
    'redaction:restore',
    (_e, req: { caseId: string; redactedText: string }) =>
      requireCore().rpc('redaction.restore', req)
  )
  ipcMain.handle(
    'redaction:listEntities',
    (_e, req: { caseId: string; status?: string }) =>
      requireCore().rpc('redaction.list_entities', req)
  )
  ipcMain.handle(
    'redaction:confirmEntities',
    (_e, req: { caseId: string; entityIds: string[]; action?: string }) =>
      requireCore().rpc('redaction.confirm_entities', req)
  )
  ipcMain.handle('redaction:listTerms', (_e, req: { caseId: string }) =>
    requireCore().rpc('redaction.list_terms', req)
  )
  ipcMain.handle('redaction:addTerm', (_e, req: { caseId: string; term: string }) =>
    requireCore().rpc('redaction.add_term', req)
  )
  ipcMain.handle('redaction:removeTerm', (_e, req: { caseId: string; termId: string }) =>
    requireCore().rpc('redaction.remove_term', req)
  )

  // ---- 技能（REQ-AI-003 / REQ-AI-007；安装走 Agent 工具 skill_install） --------

  ipcMain.handle('skill:list', () => requireCore().rpc('skill.list', {}))
  ipcMain.handle('skill:remove', (_e, req: { skillId: string }) =>
    requireCore().rpc('skill.remove', req)
  )
}

// 单实例锁（DB-010）：第二实例直接退出，避免两个 Core 同时写同一个 SQLite 库
const singleInstance = app.requestSingleInstanceLock()
if (!singleInstance) {
  app.quit()
} else {
  app.on('second-instance', () => {
    // 已有实例：聚焦既有窗口，不启动第二个 Core
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })
}

app.whenReady().then(async () => {
  if (!singleInstance) return // 第二实例不创建窗口、不启动 Core
  migrateLegacyAppData()
  loadDialogState()
  if (isDev) {
    // 清磁盘缓存：dev server 头是按 ETag 304 复用缓存的，config 里 CSP 变更后
    // 旧缓存（无 frame-src）会让 lawbuddy-preview: 的 iframe 被 ERR_BLOCKED_BY_CSP 静默拦截
    await session.defaultSession.clearCache()
  }
  void prunePreviewDisk() // 启动清理过期预览磁盘文件（异步不阻塞窗口）
  registerPreviewProtocol()
  registerMediaProtocol()
  registerIpc()

  const win = createWindow()
  mainWindow = win
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
  })

  // 启动 Python Core：握手 → 心跳 → 崩溃重启（技术方案 §2.1、§17）
  core = new CoreProcess((status) => {
    if (!win.isDestroyed()) win.webContents.send('core:status', status)
  })
  // Core 主动推送的任务事件（REQ-AI-001：推送 + 轮询兜底）
  core.on('agent-event', (n) => {
    if (!win.isDestroyed()) win.webContents.send('agent:event', n)
  })
  core.start()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createWindow()
  })
})

app.on('window-all-closed', () => {
  core?.stop()
  core = null
  if (process.platform !== 'darwin') app.quit()
})

app.on('will-quit', () => {
  core?.stop()
  core = null
})
