// 打包前凭据清除（AGENTS.md「API Key 与打包安全」）。
//
// 目标：进入安装包的产物里不得携带任何模型 / MCP / 联网工具等 API Key，
// 密钥一律由用户在应用内首次启动后自行配置（加密保存在本机 profile，不随包分发）。
//
// 处理范围（只动可重建的打包产物，不改源码与用户本地开发配置）：
//   1. 删除产物内的 .env* / 密钥载荷 / MCP 与连接器配置文件；
//   2. 对产物内的文本资源做凭据值脱敏：保留字段名与结构，清空密钥值；
//   3. 结束后由 scripts/scan-secrets.mjs 兜底扫描，命中即阻断打包。
//
// 用法：node scripts/purge-package-secrets.mjs
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join, extname, resolve, relative } from 'node:path'

const root = resolve(import.meta.dirname, '..')

// 只处理可重建的构建产物。services/core/seed 是受版本控制的源码，
// 不在此处改写（避免打包过程污染工作区），其密钥风险由 scan-secrets.mjs 拦截。
const TARGETS = [
  { dir: resolve(root, 'apps/desktop/out'), label: 'Electron 构建产物' },
  { dir: resolve(root, 'services/core/dist_core'), label: 'Python Core 冻结产物' }
]

// 直接删除的整份文件：环境变量、密钥载荷、MCP / 连接器配置（都属于用户本机配置）
const DROP_FILE = [
  /^\.env(?:\.|$)/i,
  /^builtin-key\.bin$/i,
  /^(?:mcp|mcp[_-]servers?|connectors?|providers?[_-]secrets|credentials|secrets)\.(?:json|ya?ml|toml)$/i
]

// 通用规则：HTTP 认证头 + 各厂商密钥字面量（不改字段名，只清空值）
const COMMON_RULES = [
  // Authorization: Bearer xxx / Basic xxx（值限定为不含引号花括号的 token 字符）
  { re: /\b(Authorization\s*:\s*(?:Bearer|Basic)\s+)([A-Za-z0-9][A-Za-z0-9._~+/=-]{7,})/gi, to: '$1' },
  { re: /\b(x-api-key\s*:\s*)([A-Za-z0-9][A-Za-z0-9._~+/=-]{7,})/gi, to: '$1' },
  // OpenAI 风格密钥、Tavily 等联网工具密钥
  { re: /\b(sk-)[A-Za-z0-9_-]{20,}\b/g, to: '$1' },
  { re: /\b(tvly-)[A-Za-z0-9]{10,}\b/g, to: '$1' }
]

// JSON：{"apiKey": "实际值"} → {"apiKey": ""}
const JSON_RULE = {
  re: /("(?:api[_-]?key|apiKey|access[_-]?token|refresh[_-]?token|token|secret|client[_-]?secret|password|authorization)"\s*:\s*")([^"]{8,})(")/gi,
  to: '$1$3'
}

// YAML / INI / TOML / properties：api_key: 实际值 → api_key:
// 只在配置类扩展名上启用，避免误改 Python/JS 里 api_key = 表达式之类的代码
const CONFIG_RULE = {
  re: /^(\s*(?:api[_-]?key|apiKey|access[_-]?token|token|secret|client[_-]?secret|password|authorization)\s*[:=]\s*)(?!["'{[<]|\s*$)(\S{8,})\s*$/gim,
  to: '$1'
}

const JSON_EXT = ['.json']
const CONFIG_EXT = ['.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.properties']
// 其他文本资源只应用通用规则（认证头 / 厂商密钥字面量）
const TEXT_EXT = ['.md', '.txt', '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.html', '.css', '.tpl']

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) yield* walk(full)
    else yield full
  }
}

function rulesFor(file) {
  const ext = extname(file).toLowerCase()
  if (JSON_EXT.includes(ext)) return [JSON_RULE, ...COMMON_RULES]
  if (CONFIG_EXT.includes(ext)) return [CONFIG_RULE, ...COMMON_RULES]
  if (TEXT_EXT.includes(ext)) return COMMON_RULES
  return null
}

const removed = []
const redacted = []

for (const { dir, label } of TARGETS) {
  if (!existsSync(dir)) {
    console.log(`[purge-secrets] 跳过（不存在）：${label} → ${relative(root, dir)}`)
    continue
  }
  for (const file of [...walk(dir)]) {
    const name = file.split(/[\\/]/).pop() || ''
    if (DROP_FILE.some((re) => re.test(name))) {
      rmSync(file, { force: true })
      removed.push(relative(root, file))
      continue
    }
    const rules = rulesFor(file)
    if (!rules) continue

    let text
    try {
      text = readFileSync(file, 'utf8')
    } catch {
      continue
    }
    let next = text
    for (const { re, to } of rules) {
      re.lastIndex = 0
      next = next.replace(re, to)
    }
    if (next !== text) {
      writeFileSync(file, next, 'utf8')
      redacted.push(relative(root, file))
    }
  }
}

console.log(`[purge-secrets] 删除密钥载荷文件 ${removed.length} 个`)
for (const f of removed) console.log(`  - ${f}`)
console.log(`[purge-secrets] 脱敏文本资源 ${redacted.length} 个`)
for (const f of redacted) console.log(`  - ${f}`)
console.log('[purge-secrets] 完成：安装包不携带模型 / MCP / 联网工具 API Key，由用户首次启动后自行配置')
