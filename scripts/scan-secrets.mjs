// 打包前敏感信息扫描门禁（AGENTS.md「API Key 与打包安全」）。
// 扫描将进入安装包的文本产物：Electron 构建产物与 Python Core 源码。
// 命中疑似密钥时以非零码退出，阻断 electron-builder 打包。
// 用法：node scripts/scan-secrets.mjs [--out <目录>]（默认 apps/desktop/out）
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, extname, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const outArgIdx = process.argv.indexOf('--out')
const outDir = resolve(root, outArgIdx > 0 ? process.argv[outArgIdx + 1] : 'apps/desktop/out')
const pyDir = resolve(root, 'services/core/lawbuddy')
const seedDir = resolve(root, 'services/core/seed')
// 随包分发的 seed 产物中允许出现的文本扩展名（技能资源）
const SEED_TEXT_EXT = ['.json', '.md', '.txt', '.py', '.js', '.mjs', '.ts', '.yaml', '.yml', '.html', '.css']

// 疑似密钥模式（保守：只报高置信度形态，避免误报阻断正常构建）
const PATTERNS = [
  { name: 'OpenAI 风格密钥 (sk-…)', re: /\bsk-[A-Za-z0-9_-]{20,}\b/g },
  { name: 'PEM 私钥块', re: /-----BEGIN (?:RSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----/g },
  { name: 'JWT 令牌', re: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g },
  // 代码中的字面量密钥赋值：apiKey: "实际值"（≥16 位非常量值；placeholder/configured 不匹配）
  {
    name: '字面量 API Key 赋值',
    re: /(?:api[_-]?key|secret|token|password|default[_-]?key|builtin[_-]?key)["']?\s*[:=]\s*["'](?![<{\[${]|(?:your|xxx|test|demo|example|placeholder|configured|lawbuddy)[-_ ])[A-Za-z0-9+/_\-=.]{16,}["']/gi
  }
]

// 白名单：已知安全的占位/文档片段（按行匹配后人工复核过的固定串）
const LINE_ALLOW = [
  /settings\.get\(['"]model\./, // 读取已配置状态，非密钥值
  /zhipuApiKey|topenrouterApiKey|deepseekApiKey/, // 配置键名本身
  /process\.env\./ // 环境变量引用
]

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) yield* walk(full)
    else yield full
  }
}

function collectFiles() {
  const files = []
  try {
    for (const f of walk(outDir)) {
      if (['.js', '.mjs', '.cjs', '.html', '.css', '.json'].includes(extname(f))) files.push(f)
    }
  } catch {
    console.error(`[scan-secrets] 构建产物目录不存在：${outDir}（先执行 npm run build）`)
    process.exit(2)
  }
  for (const f of walk(pyDir)) {
    if (extname(f) === '.py') files.push(f)
  }
  files.push(join(root, 'services/core/frozen_entry.py'))
  // seed 产物（seed:export 在 dist:win 链路中先于本扫描执行）；目录缺失时跳过
  try {
    for (const f of walk(seedDir)) {
      if (SEED_TEXT_EXT.includes(extname(f))) files.push(f)
    }
  } catch {
    console.error(`[scan-secrets] 提示：seed 目录不存在，跳过（${seedDir}）`)
  }
  return files
}

const offenders = []
let scanned = 0
for (const file of collectFiles()) {
  const text = readFileSync(file, 'utf8')
  scanned++
  const lines = text.split(/\r?\n/)
  for (let i = 0; i < lines.length; i++) {
    if (LINE_ALLOW.some((allow) => allow.test(lines[i]))) continue
    for (const { name, re } of PATTERNS) {
      re.lastIndex = 0
      if (re.test(lines[i])) offenders.push({ file, line: i + 1, name, snippet: lines[i].trim().slice(0, 120) })
    }
  }
}

if (offenders.length > 0) {
  console.error(`[scan-secrets] 发现 ${offenders.length} 处疑似密钥，已阻断打包：`)
  for (const o of offenders) console.error(`  - ${o.name} @ ${o.file}:${o.line}\n    ${o.snippet}`)
  process.exit(1)
}
console.log(`[scan-secrets] 通过：扫描 ${scanned} 个文件，未发现疑似密钥`)
