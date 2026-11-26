#!/usr/bin/env node
/**
 * 生成 SBOM（Software Bill of Materials，CycloneDX 1.5 JSON 子集）。
 *
 * 为什么需要：方案 WP11 要求"生成 SBOM、第三方 NOTICE 和安装包校验信息"，
 * 且发布门禁要能核对实际随包分发的依赖与许可。本脚本只读取本地已存在的
 * 清单（package-lock.json、requirements.txt、已安装 Python 环境），**不联网**。
 *
 * 用法：
 *   node scripts/gen-sbom.mjs                # 输出到 dist/sbom.cdx.json
 *   node scripts/gen-sbom.mjs --out x.json
 *   node scripts/gen-sbom.mjs --python C:\path\to\python.exe
 *
 * 设计约束：
 * - 不推断版本：Python 依赖若未安装则只记录声明约束，并标明 unresolved，
 *   不伪造具体版本号。
 * - 不记录路径、用户名或环境变量值（SBOM 会随发布物分发）。
 * - 许可证未知时写 "NOASSERTION"，绝不猜测。
 */
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'

const root = resolve(import.meta.dirname, '..')

function argsOf(argv) {
  const out = {}
  for (let i = 2; i < argv.length; i += 1) {
    if (argv[i] === '--out') out.out = argv[++i]
    else if (argv[i] === '--python') out.python = argv[++i]
  }
  return out
}

const args = argsOf(process.argv)
const outPath = resolve(root, args.out || join('dist', 'sbom.cdx.json'))

function sha256(text) {
  return createHash('sha256').update(text).digest('hex')
}

/** 读取 package-lock.json 里的实际解析版本（比 package.json 的 range 更可信）。 */
function npmComponents() {
  const lockPath = join(root, 'package-lock.json')
  if (!existsSync(lockPath)) return { components: [], notes: ['package-lock.json 缺失'] }
  const lock = JSON.parse(readFileSync(lockPath, 'utf-8'))
  const components = []
  const notes = []
  const packages = lock.packages || {}
  for (const [key, meta] of Object.entries(packages)) {
    if (!key || !key.includes('node_modules/')) continue
    if (meta.dev === true) continue // 不随发布物分发
    const name = key.slice(key.lastIndexOf('node_modules/') + 'node_modules/'.length)
    if (!name || meta.version == null) continue
    components.push({
      type: 'library',
      'bom-ref': `npm:${name}@${meta.version}`,
      name,
      version: String(meta.version),
      purl: `pkg:npm/${name}@${meta.version}`,
      licenses: [{ license: { id: meta.license || 'NOASSERTION' } }]
    })
  }
  if (components.length === 0) notes.push('package-lock 中没有可枚举的运行期依赖')
  return { components, notes }
}

/** Python：优先用已安装环境的 pip 元数据，缺失则回退声明约束（标注 unresolved）。 */
function pythonComponents(pythonExe) {
  const reqPath = join(root, 'services', 'core', 'requirements.txt')
  const declared = []
  if (existsSync(reqPath)) {
    for (const raw of readFileSync(reqPath, 'utf-8').split(/\r?\n/)) {
      const line = raw.trim()
      if (!line || line.startsWith('#')) continue
      const name = line.split(/[<>=!~[]/)[0].trim()
      if (name) declared.push({ name, spec: line })
    }
  }
  const installed = new Map()
  const candidates = [
    pythonExe,
    join(root, 'services', 'core', '.venv', 'Scripts', 'python.exe'),
    join(root, 'services', 'core', '.venv', 'bin', 'python')
  ].filter(Boolean)
  for (const exe of candidates) {
    if (!existsSync(exe)) continue
    try {
      const json = execFileSync(
        exe,
        ['-c', 'import json,importlib.metadata as m;print(json.dumps({d.metadata["Name"]:(d.version or "") for d in m.distributions()}))'],
        { encoding: 'utf-8', timeout: 60000 }
      )
      for (const [name, version] of Object.entries(JSON.parse(json))) {
        if (name) installed.set(name.toLowerCase(), String(version))
      }
      break
    } catch {
      continue
    }
  }
  const components = []
  const unresolved = []
  for (const dep of declared) {
    const version = installed.get(dep.name.toLowerCase())
    if (!version) {
      unresolved.push(dep.name)
      components.push({
        type: 'library',
        'bom-ref': `pypi:${dep.name}@unresolved`,
        name: dep.name,
        version: 'unresolved',
        purl: `pkg:pypi/${dep.name}`,
        properties: [{ name: 'lawbuddy:declaredSpec', value: dep.spec }],
        licenses: [{ license: { id: 'NOASSERTION' } }]
      })
      continue
    }
    components.push({
      type: 'library',
      'bom-ref': `pypi:${dep.name}@${version}`,
      name: dep.name,
      version,
      purl: `pkg:pypi/${dep.name}@${version}`,
      licenses: [{ license: { id: 'NOASSERTION' } }]
    })
  }
  return { components, unresolved }
}

const npm = npmComponents()
const py = pythonComponents(args.python)
const components = [...npm.components, ...py.components]
const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf-8'))

const bom = {
  bomFormat: 'CycloneDX',
  specVersion: '1.5',
  version: 1,
  metadata: {
    timestamp: new Date().toISOString(),
    component: {
      type: 'application',
      'bom-ref': `lawbuddy@${pkg.version}`,
      name: 'lawbuddy',
      version: String(pkg.version),
      licenses: [{ license: { id: pkg.license || 'Apache-2.0' } }]
    },
    tools: [{ name: 'lawbuddy-gen-sbom', version: '1.0.0' }],
    properties: [
      { name: 'lawbuddy:pythonResolved', value: String(py.components.length - py.unresolved.length) },
      { name: 'lawbuddy:pythonUnresolved', value: String(py.unresolved.length) }
    ]
  },
  components
}

mkdirSync(dirname(outPath), { recursive: true })
const text = `${JSON.stringify(bom, null, 2)}\n`
writeFileSync(outPath, text, 'utf-8')

console.log(`[gen-sbom] 组件 ${components.length} 个（npm ${npm.components.length} / python ${py.components.length}）`)
if (py.unresolved.length > 0) {
  console.log(`[gen-sbom] 未安装（版本记为 unresolved）：${py.unresolved.join(', ')}`)
}
console.log(`[gen-sbom] sha256=${sha256(text)}`)
console.log(`[gen-sbom] 已写入 ${outPath}`)
